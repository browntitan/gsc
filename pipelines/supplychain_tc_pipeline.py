"""
title: Supply Chain Internal Policy Pipeline
author: OpenAI
date: 2026-03-22
version: 1.1
license: MIT
description: Single-file supply-chain internal policy pipeline with LLM-first input extraction and direct Postgres retrieval.
requirements: psycopg[binary],requests,httpx[http2],langchain-openai
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Generator, List, Optional, Union
from urllib import error, request

import httpx
import requests
from langchain_openai import AzureOpenAIEmbeddings
from pydantic import BaseModel, Field, model_validator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Pipeline:
    CLAUSE_RE = re.compile(
        r"\b(?:clause|cl\.?|section)\s*(?:number|no\.?|#)?\s*[:\-]?\s*([0-9][A-Za-z0-9.\-()/]*)\b",
        re.IGNORECASE,
    )
    TERMSET_LABEL_RE = re.compile(
        r"\b(?:term\s*set|termset|termsets|termet|termets|t\s*&\s*c|tc|tnc|terms?\s*(?:and|&)\s*conditions?)\s*"
        r"(?:number|no\.?|id|identifier|#)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9._\-/]*)\b",
        re.IGNORECASE,
    )
    TERMSET_CODE_RE = re.compile(r"\bCTM-P-ST-(\d{1,3})\b", re.IGNORECASE)
    FOLLOWUP_CUES = (
        "explain that",
        "explain more",
        "say more",
        "expand on that",
        "what does that mean",
        "can you explain",
        "tell me more",
        "summarize that",
    )
    RESET_CUES = ("reset", "start over", "clear context", "new search", "/reset")

    class Valves(BaseModel):
        @model_validator(mode="before")
        @classmethod
        def _coerce_none_string_fields(cls, data: Any) -> Any:
            if not isinstance(data, dict):
                return data

            normalized = dict(data)
            for field_name, field_info in cls.model_fields.items():
                if normalized.get(field_name, ...) is None and field_info.annotation is str:
                    default = field_info.default
                    normalized[field_name] = default if isinstance(default, str) else ""
            return normalized

        NAME: str = Field(default=os.getenv("PIPELINE_NAME", "Supply Chain Internal Policy Pipeline"))
        DATABASE_URL: str = Field(
            default=os.getenv(
                "DATABASE_URL",
                "postgresql://openwebui:openwebui@postgres:5432/openwebui",
            )
        )
        CHUNK_TABLE_NAME: str = Field(default=os.getenv("CHUNK_TABLE_NAME", "supply_chain_chunks"))
        DEFAULT_COLLECTION_NAME: str = Field(
            default=os.getenv("DEFAULT_COLLECTION_NAME", "GSC-Internal-Policy")
        )
        TOP_K: int = Field(default=int(os.getenv("TOP_K", "6")), ge=1, le=20)
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
            ge=10,
            le=600,
        )
        ROUTER_MODE: str = Field(default=os.getenv("ROUTER_MODE", "extractor_assisted"))
        ENABLE_LLM_EXTRACTOR: bool = Field(
            default=_env_bool("ENABLE_LLM_EXTRACTOR", True)
        )
        ENABLE_LLM_FORMATTER: bool = Field(
            default=_env_bool("ENABLE_LLM_FORMATTER", True)
        )
        EXTRACTOR_TIMEOUT_SECONDS: int = Field(
            default=int(os.getenv("EXTRACTOR_TIMEOUT_SECONDS", "30")),
            ge=5,
            le=300,
        )

        ANSWER_PROVIDER: str = Field(default=os.getenv("ANSWER_PROVIDER", "ollama"))
        ANSWER_MODEL: str = Field(default=os.getenv("ANSWER_MODEL", "gpt-oss:20b"))
        EMBEDDING_PROVIDER: str = Field(default=os.getenv("EMBEDDING_PROVIDER", "ollama"))
        EMBEDDING_MODEL: str = Field(default=os.getenv("EMBEDDING_MODEL", ""))
        EMBEDDING_DIMENSIONS: int = Field(
            default=int(os.getenv("EMBEDDING_DIMENSIONS", "768")),
            ge=1,
        )

        EXTRACTOR_PROVIDER: str = Field(default=os.getenv("EXTRACTOR_PROVIDER", "ollama"))
        EXTRACTOR_MODEL: str = Field(default=os.getenv("EXTRACTOR_MODEL", "gpt-oss:20b"))
        FORMATTER_PROVIDER: str = Field(default=os.getenv("FORMATTER_PROVIDER", "ollama"))
        FORMATTER_MODEL: str = Field(default=os.getenv("FORMATTER_MODEL", "gpt-oss:20b"))

        OLLAMA_BASE_URL: str = Field(
            default=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        )
        OLLAMA_CHAT_MODEL: str = Field(default=os.getenv("OLLAMA_CHAT_MODEL", "gpt-oss:20b"))
        OLLAMA_EMBED_MODEL: str = Field(default=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
        AZURE_OPENAI_ENDPOINT: str = Field(
            default=os.getenv("AZURE_OPENAI_ENDPOINT", os.getenv("AZURE_OPENAI_BASE_URL", ""))
        )
        AZURE_OPENAI_DEPLOYMENT_NAME: str = Field(
            default=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", os.getenv("AZURE_OPENAI_CHAT_MODEL", ""))
        )
        AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME: str = Field(
            default=os.getenv(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME",
                os.getenv("AZURE_OPENAI_EMBED_MODEL", ""),
            )
        )
        AZURE_OPENAI_API_VERSION: str = Field(
            default=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
        )
        AZURE_OPENAI_BASE_URL: str = Field(default=os.getenv("AZURE_OPENAI_BASE_URL", ""))
        AZURE_OPENAI_API_KEY: str = Field(default=os.getenv("AZURE_OPENAI_API_KEY", ""))
        AZURE_OPENAI_CHAT_MODEL: str = Field(default=os.getenv("AZURE_OPENAI_CHAT_MODEL", ""))
        AZURE_OPENAI_EMBED_MODEL: str = Field(default=os.getenv("AZURE_OPENAI_EMBED_MODEL", ""))

    def __init__(self):
        self.id = "supplychain_tc_pipeline"
        self.valves = self.Valves()
        self.name = self.valves.NAME
        self._schema_cache: set[tuple[str, str, int]] = set()

    async def on_valves_updated(self):
        self.name = self.valves.NAME
        self._schema_cache.clear()

    def _status_details(self, title: str, body: str = "", done: bool = False) -> str:
        done_attr = "true" if done else "false"
        details_body = (body or "").strip()
        if details_body:
            details_body = "\n\n" + details_body + "\n"
        return (
            f'<details type="status" done="{done_attr}">\n'
            f"<summary>{title}</summary>{details_body}</details>\n"
        )

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Union[str, Generator[str, None, None]]:
        if body.get("title"):
            return self.name

        if body.get("stream"):
            return self._pipe_gen(user_message=user_message, model_id=model_id, messages=messages)

        return "".join(
            self._pipe_gen(user_message=user_message, model_id=model_id, messages=messages)
        )

    def _pipe_gen(
        self,
        *,
        user_message: str,
        model_id: str,
        messages: List[dict],
    ) -> Generator[str, None, None]:
        del user_message, model_id

        if not messages:
            yield "Please send a request with a clause number, a termset number, and your question."
            return

        user_messages = [m for m in messages if m.get("role") == "user"]
        if not user_messages:
            yield "Please send a request with a clause number, a termset number, and your question."
            return

        current_text = self._message_text(user_messages[-1])
        if not current_text:
            yield "Please send a request with a clause number, a termset number, and your question."
            return

        if self._is_reset(current_text):
            yield (
                "Search context cleared for this chat. "
                "Send a clause number, a termset number, and your question when you're ready."
            )
            return

        prior_state = self._derive_prior_state(user_messages[:-1])

        try:
            deterministic = self._extract_fields(current_text)
        except ValueError as exc:
            yield str(exc)
            return

        extractor_result: Dict[str, Optional[Any]] = {}
        formatter_result: Dict[str, Optional[Any]] = {}
        if self._should_use_extractor():
            extractor_result = self._extract_with_llm(current_text, prior_state, deterministic)
            if self._should_use_formatter():
                formatter_result = self._format_with_llm(
                    current_text=current_text,
                    prior_state=prior_state,
                    deterministic=deterministic,
                    extractor_result=extractor_result,
                )

        parsed = self._resolve_input_fields(deterministic, extractor_result, formatter_result)

        decision = self._classify_turn(
            current_text=current_text,
            parsed=parsed,
            prior_state=prior_state,
            extractor_intent=self._clean_optional(
                formatter_result.get("intent") or extractor_result.get("intent")
            ),
        )
        merged = self._merge_state(prior_state, parsed, decision)

        missing = self._missing_fields(merged)
        if missing:
            yield self._build_missing_message(merged, missing)
            return

        status_decision = self._status_decision(
            decision=decision,
            current_text=current_text,
            current_message_fields=deterministic,
            prior_state=prior_state,
        )
        search_status = self._search_status_message(status_decision, merged)
        if search_status:
            yield self._status_details("Search", search_status, done=False)

        yield self._status_details(
            "Retrieval",
            (
                f"Querying collection '{self.valves.DEFAULT_COLLECTION_NAME}' for Clause "
                f"{merged['clause_number']} under termset {merged['termset_number']}."
            ),
            done=False,
        )

        try:
            hits = self._search_guidance(
                collection_name=self.valves.DEFAULT_COLLECTION_NAME,
                clause_number=merged["clause_number"],
                termset_number=merged["termset_number"],
                query=merged["query_text"],
                top_k=self.valves.TOP_K,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"Search failed before retrieval could complete: {exc}"
            yield self._status_details("Error", message, done=True)
            yield message
            return

        if not hits:
            message = (
                f"I didn't find guidance for Clause {merged['clause_number']} "
                f"under termset {merged['termset_number']} in collection "
                f"'{self.valves.DEFAULT_COLLECTION_NAME}'. "
                "Please confirm the identifiers or try a different clause or termset."
            )
            yield self._status_details(
                "No Hits",
                (
                    f"No guidance matched Clause {merged['clause_number']} under termset "
                    f"{merged['termset_number']} in collection '{self.valves.DEFAULT_COLLECTION_NAME}'."
                ),
                done=True,
            )
            yield message
            return

        provenance_notice = self._provenance_notice(hits)
        prompt_messages = self._build_grounding_messages(
            merged=merged,
            user_text=current_text,
            hits=hits,
            followup=(decision == "followup_explain"),
            collection_name=self.valves.DEFAULT_COLLECTION_NAME,
        )

        try:
            answer = self._generate_answer(prompt_messages)
        except Exception as exc:  # noqa: BLE001
            yield self._status_details(
                "Error",
                "Model generation failed. Returning a deterministic fallback summary instead.",
                done=True,
            )
            answer = self._fallback_answer(hits, merged)
            answer += (
                "\n\n(Note: model generation failed, so I returned a deterministic "
                f"fallback summary: {exc})"
            )
        else:
            yield self._status_details(
                "Done",
                (
                    f"Retrieved {len(hits)} guidance chunk(s) from collection "
                    f"'{self.valves.DEFAULT_COLLECTION_NAME}'."
                ),
                done=True,
            )

        if provenance_notice:
            answer = provenance_notice + "\n\n" + answer
        answer = answer.rstrip() + "\n\n" + self._render_retrieved_chunk_citations(hits)
        yield answer
        return

    def _message_text(self, message: dict) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return " ".join(parts).strip()
        return str(content).strip()

    def _is_reset(self, text: str) -> bool:
        lowered = text.lower().strip()
        return any(cue in lowered for cue in self.RESET_CUES)

    def _extract_fields(self, text: str) -> Dict[str, Optional[str]]:
        clause_matches = self.CLAUSE_RE.findall(text)
        label_termset_matches = self.TERMSET_LABEL_RE.findall(text)
        code_termset_matches = self.TERMSET_CODE_RE.findall(text)
        raw_termset_matches = [*label_termset_matches, *code_termset_matches]

        if len(clause_matches) > 1 and "compare" not in text.lower():
            raise ValueError(
                "I saw more than one clause number in that message. "
                "This demo handles one clause at a time. Please send one clause number."
            )
        if len(raw_termset_matches) > 1 and "compare" not in text.lower():
            normalized = {
                self._normalize_termset_number(value)
                for value in raw_termset_matches
                if self._normalize_termset_number(value)
            }
            if len(normalized) > 1:
                raise ValueError(
                    "I saw more than one termset number in that message. "
                    "This demo handles one termset at a time. Please send one termset number."
                )

        clause_number = self._normalize_clause_number(clause_matches[0]) if clause_matches else None
        termset_number = None
        if raw_termset_matches:
            termset_number = self._normalize_termset_number(raw_termset_matches[0])

        temp = text
        clause_match = self.CLAUSE_RE.search(text)
        if clause_match:
            temp = temp.replace(clause_match.group(0), " ")

        for match in self.TERMSET_CODE_RE.finditer(text):
            temp = temp.replace(match.group(0), " ")
        for match in self.TERMSET_LABEL_RE.finditer(text):
            temp = temp.replace(match.group(0), " ")

        temp = re.sub(
            r"(?i)\b(?:please|now|can you|could you|would you|check|review|look at|analyze|search|find|tell me|show me|what does|what is|what's|how does|explain)\b",
            " ",
            temp,
        )
        temp = re.sub(r"(?i)\bsay about\b", " ", temp)
        temp = re.sub(r"(?i)\bfor\b\s*$", " ", temp)
        temp = re.sub(r"(?i)\bunder\b\s*$", " ", temp)
        temp = re.sub(r"(?i)\babout\b\s*$", " ", temp)
        temp = re.sub(r"\s+", " ", temp).strip(" -:,.?")

        lowered = text.lower().strip()
        is_followup = any(cue in lowered for cue in self.FOLLOWUP_CUES)
        query_text = None if is_followup else (temp if len(temp) >= 3 else None)
        query_text = self._clean_query_text(query_text)

        return {
            "clause_number": clause_number,
            "termset_number": termset_number,
            "query_text": query_text,
        }

    def _derive_prior_state(self, user_messages: List[dict]) -> Dict[str, Optional[str]]:
        state = self._empty_state()
        for message in user_messages:
            text = self._message_text(message)
            if self._is_reset(text):
                state = self._empty_state()
                continue

            try:
                parsed = self._extract_fields(text)
            except Exception:
                continue

            decision = self._classify_turn(
                current_text=text,
                parsed=parsed,
                prior_state=state,
                extractor_intent=None,
            )
            state = self._merge_state(state, parsed, decision)
        return state

    def _empty_state(self) -> Dict[str, Optional[str]]:
        return {"clause_number": None, "termset_number": None, "query_text": None}

    def _should_use_extractor(self) -> bool:
        return (
            self.valves.ROUTER_MODE.strip().lower() == "extractor_assisted"
            and self.valves.ENABLE_LLM_EXTRACTOR
        )

    def _should_use_formatter(self) -> bool:
        return (
            self.valves.ROUTER_MODE.strip().lower() == "extractor_assisted"
            and self.valves.ENABLE_LLM_FORMATTER
        )

    def _resolve_input_fields(
        self,
        deterministic: Dict[str, Optional[str]],
        extractor_result: Dict[str, Optional[Any]],
        formatter_result: Dict[str, Optional[Any]],
    ) -> Dict[str, Optional[str]]:
        preferred = formatter_result or extractor_result or {}

        clause_candidate = self._normalize_clause_number(preferred.get("clause_number"))
        termset_candidate = self._normalize_termset_number(preferred.get("termset_number"))
        deterministic_clause = self._normalize_clause_number(deterministic.get("clause_number"))
        deterministic_termset = self._normalize_termset_number(deterministic.get("termset_number"))

        if deterministic_clause and clause_candidate and clause_candidate != deterministic_clause:
            clause_candidate = deterministic_clause
        if deterministic_termset and termset_candidate and termset_candidate != deterministic_termset:
            termset_candidate = deterministic_termset

        clause_number = clause_candidate or deterministic_clause
        termset_number = termset_candidate or deterministic_termset
        query_text = self._clean_query_text(
            preferred.get("query_text") or deterministic.get("query_text")
        )

        return {
            "clause_number": clause_number,
            "termset_number": termset_number,
            "query_text": query_text,
        }

    def _classify_turn(
        self,
        current_text: str,
        parsed: Dict[str, Optional[str]],
        prior_state: Dict[str, Optional[str]],
        extractor_intent: Optional[str] = None,
    ) -> str:
        lowered = current_text.lower().strip()
        has_prior_complete = all(
            prior_state.get(k) for k in ("clause_number", "termset_number", "query_text")
        )
        explicit_ids = bool(parsed.get("clause_number") or parsed.get("termset_number"))
        has_query = bool(parsed.get("query_text"))
        is_followup = any(cue in lowered for cue in self.FOLLOWUP_CUES) or extractor_intent == "followup_explain"

        if extractor_intent in {"new_search", "identifier_update", "same_context_new_query"}:
            if extractor_intent == "same_context_new_query" and not has_query:
                return "collect_or_search"
            return extractor_intent

        if is_followup and has_prior_complete and not explicit_ids and not has_query:
            return "followup_explain"

        if explicit_ids and has_query:
            return "new_search"

        if explicit_ids and not has_query:
            return "identifier_update"

        if has_query and has_prior_complete and not explicit_ids:
            return "same_context_new_query"

        return "collect_or_search"

    def _status_decision(
        self,
        *,
        decision: str,
        current_text: str,
        current_message_fields: Dict[str, Optional[str]],
        prior_state: Dict[str, Optional[str]],
    ) -> str:
        if decision in {"same_context_new_query", "followup_explain", "identifier_update"}:
            return decision

        lowered = current_text.lower().strip()
        has_prior_complete = all(
            prior_state.get(k) for k in ("clause_number", "termset_number", "query_text")
        )
        explicit_ids = bool(
            current_message_fields.get("clause_number")
            or current_message_fields.get("termset_number")
        )
        has_query = bool(current_message_fields.get("query_text"))
        is_followup = any(cue in lowered for cue in self.FOLLOWUP_CUES)

        if has_prior_complete and not explicit_ids:
            if is_followup and not has_query:
                return "followup_explain"
            return "same_context_new_query"

        return decision

    def _merge_state(
        self,
        prior_state: Dict[str, Optional[str]],
        parsed: Dict[str, Optional[str]],
        decision: str,
    ) -> Dict[str, Optional[str]]:
        merged = {
            "clause_number": prior_state.get("clause_number"),
            "termset_number": prior_state.get("termset_number"),
            "query_text": prior_state.get("query_text"),
        }

        if parsed.get("clause_number"):
            merged["clause_number"] = parsed["clause_number"]
        if parsed.get("termset_number"):
            merged["termset_number"] = parsed["termset_number"]
        if parsed.get("query_text"):
            merged["query_text"] = parsed["query_text"]

        if decision == "followup_explain" and not parsed.get("query_text"):
            merged["query_text"] = prior_state.get("query_text")

        return merged

    def _missing_fields(self, merged: Dict[str, Optional[str]]) -> List[str]:
        missing = []
        if not merged.get("clause_number"):
            missing.append("clause_number")
        if not merged.get("termset_number"):
            missing.append("termset_number")
        if not merged.get("query_text"):
            missing.append("query_text")
        return missing

    def _build_missing_message(self, merged: Dict[str, Optional[str]], missing: List[str]) -> str:
        missing_set = set(missing)

        if missing_set == {"clause_number"}:
            return (
                f"I have termset {merged['termset_number']} and your question. "
                "What clause number should I use?"
            )

        if missing_set == {"termset_number"}:
            return (
                f"I have Clause {merged['clause_number']} and your question. "
                "Which termset number should I search under?"
            )

        if missing_set == {"query_text"}:
            return (
                f"I have Clause {merged['clause_number']} under termset {merged['termset_number']}. "
                "What would you like to know about it?"
            )

        if missing_set == {"clause_number", "termset_number"}:
            return "I need the clause number and the termset number before I can search. What are they?"

        if missing_set == {"clause_number", "query_text"}:
            return (
                f"I already have termset {merged['termset_number']}. "
                "What clause number should I use, and what would you like to know about it?"
            )

        if missing_set == {"termset_number", "query_text"}:
            return (
                f"I already have Clause {merged['clause_number']}. "
                "Which termset number should I use, and what would you like to know about it?"
            )

        return (
            "To run the search I need all three inputs: "
            "Clause Number, Termset Number, and your question."
        )

    def _search_status_message(self, decision: str, merged: Dict[str, str]) -> str:
        if decision == "new_search":
            return (
                f"Starting a new search in collection '{self.valves.DEFAULT_COLLECTION_NAME}' "
                f"with Clause {merged['clause_number']} under termset {merged['termset_number']}. "
            )
        if decision in {"same_context_new_query", "followup_explain"}:
            return (
                f"Using your current context in collection '{self.valves.DEFAULT_COLLECTION_NAME}' "
                f"for Clause {merged['clause_number']} under termset {merged['termset_number']}. "
            )
        if decision == "identifier_update":
            return (
                f"Using the updated identifiers in collection '{self.valves.DEFAULT_COLLECTION_NAME}' "
                f"for Clause {merged['clause_number']} under termset {merged['termset_number']}. "
            )
        return ""

    def _extract_with_llm(
        self,
        current_text: str,
        prior_state: Dict[str, Optional[str]],
        deterministic: Dict[str, Optional[str]],
    ) -> Dict[str, Optional[Any]]:
        messages = [
            {
                "role": "system",
                "content": (
                    "Extract structured fields for a supply-chain internal policy retrieval workflow. "
                    "The user may refer to a termset as 'termset', 'termet', 'T&C', or a full code like CTM-P-ST-001. "
                    "Return JSON only with keys: clause_number, termset_number, query_text, intent, has_required_inputs. "
                    "Intent must be one of followup_explain, new_search, identifier_update, same_context_new_query, collect_or_search. "
                    "Use null when a field cannot be confidently extracted."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_text": current_text,
                        "prior_state": prior_state,
                        "deterministic_hints": deterministic,
                    }
                ),
            },
        ]

        try:
            provider = self._resolve_provider(
                self.valves.EXTRACTOR_PROVIDER,
                fallback=self.valves.ANSWER_PROVIDER,
            )
            raw = self._chat_completion(
                provider=provider,
                model=self._resolve_chat_model(provider, self.valves.EXTRACTOR_MODEL, "extractor"),
                messages=messages,
                temperature=0.0,
                timeout=self.valves.EXTRACTOR_TIMEOUT_SECONDS,
            )
            return self._parse_json_object(raw)
        except Exception:
            return {}

    def _format_with_llm(
        self,
        *,
        current_text: str,
        prior_state: Dict[str, Optional[str]],
        deterministic: Dict[str, Optional[str]],
        extractor_result: Dict[str, Optional[Any]],
    ) -> Dict[str, Optional[Any]]:
        messages = [
            {
                "role": "system",
                "content": (
                    "Format and normalize retrieval inputs for a supply-chain internal policy assistant. "
                    "Return JSON only with keys: clause_number, termset_number, query_text, intent, has_required_inputs. "
                    "Normalize termset_number to a three-digit string when possible, such as 1 -> 001 or CTM-P-ST-001 -> 001. "
                    "Keep query_text concise and searchable. "
                    "If you are unsure, preserve the deterministic hints."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_text": current_text,
                        "prior_state": prior_state,
                        "deterministic_hints": deterministic,
                        "extractor_result": extractor_result,
                    }
                ),
            },
        ]

        try:
            provider = self._resolve_provider(
                self.valves.FORMATTER_PROVIDER,
                fallback=self.valves.EXTRACTOR_PROVIDER or self.valves.ANSWER_PROVIDER,
            )
            raw = self._chat_completion(
                provider=provider,
                model=self._resolve_chat_model(
                    provider,
                    self.valves.FORMATTER_MODEL or self.valves.EXTRACTOR_MODEL,
                    "formatter",
                ),
                messages=messages,
                temperature=0.0,
                timeout=self.valves.EXTRACTOR_TIMEOUT_SECONDS,
            )
            return self._parse_json_object(raw)
        except Exception:
            return {}

    def _parse_json_object(self, raw: str) -> Dict[str, Optional[Any]]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}

        result: Dict[str, Optional[Any]] = {}
        for key in ("clause_number", "termset_number", "query_text", "intent"):
            value = data.get(key)
            if isinstance(value, str):
                value = value.strip() or None
            elif value is not None:
                value = str(value).strip() or None
            result[key] = value

        has_required = data.get("has_required_inputs")
        if isinstance(has_required, bool):
            result["has_required_inputs"] = has_required
        elif isinstance(has_required, str):
            result["has_required_inputs"] = has_required.strip().lower() in {"1", "true", "yes", "on"}
        else:
            result["has_required_inputs"] = None
        return result

    def _search_guidance(
        self,
        *,
        collection_name: str,
        clause_number: str,
        termset_number: str,
        query: str,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        query_embedding = self._embed_text(query)
        table = self._table_identifier()
        query_sql = sql.SQL(
            """
            SELECT
                id::text,
                collection_name,
                external_id,
                clause_number,
                tc_number,
                topic,
                source_doc,
                section_title,
                chunk_text,
                guidance_text,
                metadata,
                1 - (embedding <=> %s::vector) AS score
            FROM {table}
            WHERE collection_name = %s
              AND clause_number_norm = %s
              AND tc_number_norm = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """
        ).format(table=table)

        vec = self._vector_literal(query_embedding)
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query_sql,
                    (
                        vec,
                        collection_name,
                        self._normalize_identifier(clause_number),
                        self._normalize_identifier(termset_number),
                        vec,
                        top_k,
                    ),
                )
                return cur.fetchall()

    def _build_grounding_messages(
        self,
        merged: Dict[str, str],
        user_text: str,
        hits: List[dict],
        followup: bool,
        collection_name: str,
    ) -> List[dict]:
        source_blocks = []
        for idx, hit in enumerate(hits, start=1):
            snippet = (hit.get("chunk_text") or hit.get("guidance_text") or "").strip()
            source_blocks.append(
                f"[S{idx}] "
                f"{hit.get('source_doc', 'unknown-source')} | "
                f"{hit.get('section_title', 'untitled')} | "
                f"collection={hit.get('collection_name', collection_name)} | "
                f"termset={hit.get('tc_number', 'unknown')} | "
                f"score={round(float(hit.get('score', 0.0)), 4)}\n"
                f"{snippet}"
            )

        followup_instruction = (
            "The user is asking a follow-up explanation. Reuse the current clause and termset context and explain the guidance more clearly."
            if followup
            else "Answer the user's current retrieval question."
        )

        system = (
            "You are an internal supply-chain policy guidance assistant. "
            "Answer only from the retrieved guidance below. "
            "If the evidence is insufficient, say so clearly. "
            "If a retrieved source directly states the answer, summarize it plainly instead of saying the text is missing. "
            "If a source includes visible wording plus [unclear] markers, use the visible wording and note only the unclear tail if it matters. "
            "For chunks that contain 'Issue:' and 'Response:', treat the Response text as the primary answer. "
            "Do not invent policy. "
            "Cite every substantive claim with source tags like [S1] or [S2]."
        )

        user = (
            f"{followup_instruction}\n\n"
            f"Request context:\n"
            f"- Collection: {collection_name}\n"
            f"- Clause Number: {merged['clause_number']}\n"
            f"- Termset Number: {merged['termset_number']}\n"
            f"- Active question: {merged['query_text']}\n"
            f"- Current user message: {user_text}\n\n"
            f"Retrieved guidance:\n\n" + "\n\n".join(source_blocks)
        )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _provenance_notice(self, hits: List[dict]) -> str:
        for hit in hits:
            metadata = hit.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}
            source_status = str(metadata.get("source_status", "")).strip().lower()
            if source_status == "template_placeholder":
                return (
                    "Note: this result comes from placeholder/template clause content in the demo repository, "
                    "not authoritative internal policy text."
                )
            if source_status == "synthetic_demo":
                return (
                    "Note: this result comes from synthetic demo clause content in the repository, "
                    "not authoritative internal policy text."
                )
        return ""

    def _render_retrieved_chunk_citations(self, hits: List[dict]) -> str:
        lines = ["### Retrieved Chunks"]
        for idx, hit in enumerate(hits, start=1):
            metadata = hit.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}

            source_doc = self._clean_optional(hit.get("source_doc")) or "unknown-source"
            section_title = self._clean_optional(hit.get("section_title")) or "untitled"
            external_id = self._clean_optional(hit.get("external_id")) or "unknown"
            score_raw = hit.get("score")
            try:
                score = f"{float(score_raw):.4f}"
            except (TypeError, ValueError):
                score = "unknown"

            chunk_text = self._clean_optional(hit.get("chunk_text") or hit.get("guidance_text")) or ""
            excerpt = re.sub(r"\s+", " ", chunk_text).strip()
            if len(excerpt) > 280:
                excerpt = excerpt[:277].rstrip() + "..."

            segment_title = self._clean_optional(metadata.get("segment_title"))
            chunk_index = metadata.get("chunk_index")

            lines.append(
                f"[S{idx}] {source_doc} | {section_title} | external_id={external_id} | score={score}"
            )
            if segment_title:
                lines.append(f"Segment: {segment_title}")
            if chunk_index is not None:
                lines.append(f"Chunk Index: {chunk_index}")
            if excerpt:
                lines.append(f"Excerpt: {excerpt}")
            lines.append("")

        return "\n".join(lines).rstrip()

    def _generate_answer(self, messages: List[dict]) -> str:
        provider = self._resolve_provider(self.valves.ANSWER_PROVIDER)
        return self._chat_completion(
            provider=provider,
            model=self._resolve_chat_model(provider, self.valves.ANSWER_MODEL, "answer"),
            messages=messages,
            temperature=0.1,
            timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
        )

    def _clean_optional(self, value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def _clean_query_text(self, value: Optional[Any]) -> Optional[str]:
        cleaned = self._clean_optional(value)
        if not cleaned:
            return None
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:,.?")
        return cleaned or None

    def _require_config(self, value: Optional[Any], name: str) -> str:
        cleaned = self._clean_optional(value)
        if not cleaned:
            raise RuntimeError(f"{name} must be set")
        return cleaned

    def _azure_endpoint_root(self) -> str:
        endpoint = self._clean_optional(self.valves.AZURE_OPENAI_ENDPOINT) or self._clean_optional(
            self.valves.AZURE_OPENAI_BASE_URL
        )
        endpoint = self._require_config(
            endpoint,
            "AZURE_OPENAI_ENDPOINT",
        ).rstrip("/")
        if "/openai/" in endpoint.lower():
            endpoint = endpoint[: endpoint.lower().index("/openai/")]
        return endpoint.rstrip("/")

    def _azure_api_version(self) -> str:
        return self._require_config(self.valves.AZURE_OPENAI_API_VERSION, "AZURE_OPENAI_API_VERSION")

    def _azure_headers(self) -> Dict[str, str]:
        return {
            "api-key": self._require_config(self.valves.AZURE_OPENAI_API_KEY, "AZURE_OPENAI_API_KEY"),
            "Content-Type": "application/json",
        }

    def _azure_chat_url(self, deployment: str) -> str:
        return (
            f"{self._azure_endpoint_root()}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={self._azure_api_version()}"
        )

    def _azure_embedding_url(self, deployment: str) -> str:
        return (
            f"{self._azure_endpoint_root()}/openai/deployments/{deployment}/embeddings"
            f"?api-version={self._azure_api_version()}"
        )

    def _post_json_requests(
        self,
        url: str,
        payload: dict,
        headers: Optional[dict] = None,
        timeout: int = 120,
        verify: bool = False,
    ) -> dict:
        response = requests.post(
            url=url,
            json=payload,
            headers={"Content-Type": "application/json", **(headers or {})},
            timeout=timeout,
            verify=verify,
        )
        try:
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"HTTP {response.status_code} from {url}: {response.text}") from exc
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"Error decoding JSON from {url}: {exc}") from exc

    def _resolve_provider(self, value: Optional[str], fallback: str = "ollama") -> str:
        raw = self._clean_optional(value) or fallback
        normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "ollama": "ollama",
            "azure_openai": "azure_openai",
            "azure": "azure_openai",
            "openai_azure": "azure_openai",
        }
        provider = aliases.get(normalized)
        if not provider:
            raise RuntimeError(
                f"Unsupported provider '{raw}'. Use 'ollama' or 'azure_openai'."
            )
        return provider

    def _resolve_chat_model(self, provider: str, override: Optional[str], role: str) -> str:
        explicit = self._clean_optional(override)
        if explicit:
            return explicit

        if provider == "ollama":
            model = self._clean_optional(self.valves.OLLAMA_CHAT_MODEL)
            if model:
                return model
            raise RuntimeError(f"OLLAMA_CHAT_MODEL must be set when {role} provider is ollama")

        if provider == "azure_openai":
            model = self._clean_optional(self.valves.AZURE_OPENAI_DEPLOYMENT_NAME) or self._clean_optional(
                self.valves.AZURE_OPENAI_CHAT_MODEL
            )
            if model:
                return model
            raise RuntimeError(
                f"AZURE_OPENAI_DEPLOYMENT_NAME must be set when {role} provider is azure_openai"
            )

        raise RuntimeError(f"Unsupported provider: {provider}")

    def _resolve_embedding_model(self, provider: str, override: Optional[str]) -> str:
        explicit = self._clean_optional(override)
        if explicit:
            return explicit

        if provider == "ollama":
            model = self._clean_optional(self.valves.OLLAMA_EMBED_MODEL)
            if model:
                return model
            raise RuntimeError("OLLAMA_EMBED_MODEL must be set when embedding provider is ollama")

        if provider == "azure_openai":
            model = self._clean_optional(
                self.valves.AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME
            ) or self._clean_optional(self.valves.AZURE_OPENAI_EMBED_MODEL)
            if model:
                return model
            raise RuntimeError(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME must be set when embedding provider is azure_openai"
            )

        raise RuntimeError(f"Unsupported provider: {provider}")

    def _chat_completion(
        self,
        *,
        provider: str,
        model: str,
        messages: List[dict],
        temperature: float,
        timeout: int,
    ) -> str:
        if provider == "ollama":
            if not self._clean_optional(self.valves.OLLAMA_BASE_URL):
                raise RuntimeError("OLLAMA_BASE_URL must be set when using ollama")
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature},
            }
            response = self._post_json(
                self.valves.OLLAMA_BASE_URL.rstrip("/") + "/api/chat",
                payload,
                timeout=timeout,
            )
            content = (response.get("message") or {}).get("content", "").strip()
            if not content:
                raise RuntimeError("Ollama returned an empty response")
            return content

        if provider == "azure_openai":
            payload = {
                "messages": messages,
                "temperature": temperature,
            }
            response = self._post_json_requests(
                self._azure_chat_url(model),
                payload,
                headers=self._azure_headers(),
                timeout=timeout,
                verify=False,
            )
            choices = response.get("choices") or []
            if not choices:
                raise RuntimeError("Azure OpenAI returned no choices")
            content = (choices[0].get("message") or {}).get("content", "").strip()
            if not content:
                raise RuntimeError("Azure OpenAI returned empty content")
            return content

        raise RuntimeError(f"Unsupported provider: {provider}")

    def _embed_text(self, text: str) -> List[float]:
        provider = self._resolve_provider(self.valves.EMBEDDING_PROVIDER)
        model = self._resolve_embedding_model(provider, self.valves.EMBEDDING_MODEL)
        if provider == "ollama":
            if not self._clean_optional(self.valves.OLLAMA_BASE_URL):
                raise RuntimeError("OLLAMA_BASE_URL must be set when embedding provider is ollama")
            payload = {
                "model": model,
                "input": text,
                "truncate": True,
                "dimensions": self.valves.EMBEDDING_DIMENSIONS,
            }
            response = self._post_json(
                self.valves.OLLAMA_BASE_URL.rstrip("/") + "/api/embed",
                payload,
                timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
            )
            embeddings = response.get("embeddings") or []
            if not embeddings:
                raise RuntimeError("Ollama returned no embeddings")
            vector = embeddings[0]
            self._validate_dimensions(vector)
            return vector

        if provider == "azure_openai":
            os.environ["AZURE_OPENAI_API_KEY"] = self._require_config(
                self.valves.AZURE_OPENAI_API_KEY,
                "AZURE_OPENAI_API_KEY",
            )
            os.environ["AZURE_OPENAI_ENDPOINT"] = self._azure_endpoint_root()

            httpx_client = httpx.Client(
                http2=True,
                verify=False,
                timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
            )
            try:
                embeddings = AzureOpenAIEmbeddings(
                    azure_deployment=model,
                    api_version=self._azure_api_version(),
                    http_client=httpx_client,
                )
                vector = embeddings.embed_query(text)
            finally:
                httpx_client.close()
            self._validate_dimensions(vector)
            return vector

        raise RuntimeError(f"Unsupported EMBEDDING_PROVIDER: {self.valves.EMBEDDING_PROVIDER}")

    def _validate_dimensions(self, vector: List[float]) -> None:
        if len(vector) != self.valves.EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {self.valves.EMBEDDING_DIMENSIONS}, got {len(vector)}"
            )

    def _fallback_answer(self, hits: List[dict], merged: Dict[str, str]) -> str:
        top = hits[: min(3, len(hits))]
        lines = []
        for idx, hit in enumerate(top, start=1):
            snippet = (hit.get("guidance_text") or hit.get("chunk_text") or "").strip()
            if len(snippet) > 260:
                snippet = snippet[:257] + "..."
            lines.append(f"- [S{idx}] {snippet}")
        return (
            f"Based on the retrieved guidance for Clause {merged['clause_number']} under termset {merged['termset_number']}, "
            "the strongest support is:\n" + "\n".join(lines)
        )

    def _post_json(
        self,
        url: str,
        payload: dict,
        headers: Optional[dict] = None,
        timeout: int = 120,
    ) -> dict:
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Error calling {url}: {exc}") from exc

    def _get_connection(self) -> psycopg.Connection:
        return psycopg.connect(self.valves.DATABASE_URL, row_factory=dict_row)

    def _table_identifier(self):
        return sql.Identifier(self._validate_table_name(self.valves.CHUNK_TABLE_NAME))

    def _validate_table_name(self, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise RuntimeError(f"Invalid table name: {value}")
        return value

    def _ensure_schema(self) -> None:
        key = (
            self.valves.DATABASE_URL,
            self.valves.CHUNK_TABLE_NAME,
            self.valves.EMBEDDING_DIMENSIONS,
        )
        if key in self._schema_cache:
            return

        table_name = self._validate_table_name(self.valves.CHUNK_TABLE_NAME)
        unique_idx = f"sc_chunks_uq_{hashlib.sha1(table_name.encode()).hexdigest()[:8]}"
        filter_idx = f"sc_chunks_filter_{hashlib.sha1((table_name + '_f').encode()).hexdigest()[:8]}"
        vector_idx = f"sc_chunks_hnsw_{hashlib.sha1((table_name + '_v').encode()).hexdigest()[:8]}"

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {table} (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            collection_name TEXT NOT NULL,
                            external_id TEXT NOT NULL,
                            clause_number TEXT NOT NULL,
                            clause_number_norm TEXT NOT NULL,
                            tc_number TEXT NOT NULL,
                            tc_number_norm TEXT NOT NULL,
                            topic TEXT,
                            source_doc TEXT,
                            section_title TEXT,
                            chunk_text TEXT NOT NULL,
                            guidance_text TEXT,
                            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                            embedding VECTOR({dimensions}) NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    ).format(
                        table=sql.Identifier(table_name),
                        dimensions=sql.SQL(str(self.valves.EMBEDDING_DIMENSIONS)),
                    )
                )
                cur.execute(
                    sql.SQL(
                        "CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} (collection_name, external_id)"
                    ).format(
                        index_name=sql.Identifier(unique_idx),
                        table=sql.Identifier(table_name),
                    )
                )
                cur.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {index_name} ON {table} (collection_name, clause_number_norm, tc_number_norm)"
                    ).format(
                        index_name=sql.Identifier(filter_idx),
                        table=sql.Identifier(table_name),
                    )
                )
                cur.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {index_name} ON {table} USING hnsw (embedding vector_cosine_ops)"
                    ).format(
                        index_name=sql.Identifier(vector_idx),
                        table=sql.Identifier(table_name),
                    )
                )
            conn.commit()

        self._schema_cache.add(key)

    def _normalize_clause_number(self, value: Optional[Any]) -> Optional[str]:
        cleaned = self._clean_optional(value)
        if not cleaned:
            return None
        match = re.search(r"(\d+(?:\.\d+)*)", cleaned)
        if not match:
            return cleaned
        normalized = match.group(1)
        if normalized.isdigit():
            return str(int(normalized))
        return normalized

    def _normalize_termset_number(self, value: Optional[Any]) -> Optional[str]:
        cleaned = self._clean_optional(value)
        if not cleaned:
            return None
        upper = cleaned.upper()
        code_match = re.search(r"CTM-P-ST-(\d{1,3})", upper)
        if code_match:
            return code_match.group(1).zfill(3)
        digit_match = re.search(r"(\d{1,3})", upper)
        if digit_match:
            return digit_match.group(1).zfill(3)
        return None

    def _normalize_identifier(self, value: str) -> str:
        value = value.strip().upper()
        value = re.sub(r"\s+", "", value)
        return value

    def _vector_literal(self, values: List[float]) -> str:
        return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"
