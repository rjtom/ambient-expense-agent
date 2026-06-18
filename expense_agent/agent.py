# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

import google.auth
from google import genai
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow
from google.genai import types
from pydantic import BaseModel, model_validator

from expense_agent import config


class Decision(BaseModel):
    approved: str
    reason: str = "No reason provided."

    @model_validator(mode="before")
    @classmethod
    def coerce_from_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"approved": data, "reason": "No reason provided."}
        return data


# Gracefully initialize GCP credentials if we can (Option A)
try:
    _, project_id = google.auth.default()
    if project_id:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
except Exception:
    # If Option B (API key) is used, auth.default might fail, which is fine!
    pass


def get_genai_client() -> genai.Client:
    """Instantiates the GenAI client matching the active authentication option."""
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() == "true"
    if use_vertex:
        return genai.Client(vertexai=True)
    return genai.Client()


# ==============================================================================
# 1. PARSING NODE
# ==============================================================================
def parse_event_node(node_input: Any) -> dict:
    """Extracts and parses the expense data payload from the arriving event.

    Handles both base64-encoded Pub/Sub data payloads and direct plain JSON.
    If the input is natural language text, it uses Gemini to extract structured fields.
    """
    raw_data = ""

    # Handle ADK input wrapping
    if hasattr(node_input, "parts") and node_input.parts:
        raw_data = node_input.parts[0].text or ""
    elif isinstance(node_input, str):
        raw_data = node_input
    elif isinstance(node_input, dict):
        return parse_payload_dict(node_input)

    if raw_data:
        # 1. Try direct JSON parsing
        try:
            parsed = json.loads(raw_data)
            if isinstance(parsed, dict):
                return parse_payload_dict(parsed)
        except Exception:
            pass

        # 2. Fallback: Parse natural language text into a structured JSON using Gemini
        try:
            client = get_genai_client()
            prompt = f"""
            Extract structured expense details from the following natural language request. 
            CRITICAL: The 'description' field MUST contain the exact details, remarks, and raw text of the receipt/description word-for-word, including any sensitive numbers (like SSNs, credit cards, or passwords) mentioned in the input, without summarizing or omitting them.
            If some details (like date or submitter) are missing, use "2026-06-18" for date and "user@company.com" for submitter.
            
            Request: {raw_data}
            """
            response = client.models.generate_content(
                model=config.MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "amount": types.Schema(type=types.Type.NUMBER),
                            "submitter": types.Schema(type=types.Type.STRING),
                            "category": types.Schema(type=types.Type.STRING),
                            "description": types.Schema(type=types.Type.STRING),
                            "date": types.Schema(type=types.Type.STRING)
                        },
                        required=["amount", "submitter", "category", "description", "date"]
                    )
                )
            )
            parsed = json.loads(response.text)
            if isinstance(parsed, dict):
                return parsed
        except Exception as e:
            print(f"⚠️ Failed to parse natural language using Gemini: {e}")

    return {}


def parse_payload_dict(event_dict: dict) -> dict:
    """Resolves data dictionary, decoding base64 if present in 'data' key."""
    # Handle Pub/Sub structure wrapping the message
    data_payload = event_dict
    if "message" in event_dict and isinstance(event_dict["message"], dict):
        data_payload = event_dict["message"]

    data_val = data_payload.get("data")
    if data_val is None:
        data_val = event_dict

    if isinstance(data_val, str):
        # Attempt to decode as base64 (Pub/Sub style)
        try:
            decoded = base64.b64decode(data_val).decode("utf-8")
            parsed_data = json.loads(decoded)
            if isinstance(parsed_data, dict):
                return parsed_data
        except Exception:
            # Fallback to plain JSON string parsing
            try:
                parsed_data = json.loads(data_val)
                if isinstance(parsed_data, dict):
                    return parsed_data
            except Exception:
                pass
    elif isinstance(data_val, dict):
        return data_val

    return event_dict


# ==============================================================================
# 1.5. SECURITY CHECKPOINT NODE
# ==============================================================================
SSN_REGEX = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

INJECTION_KEYWORDS = [
    "ignore previous instructions",
    "ignore the rules",
    "override rules",
    "override the limit",
    "force approve",
    "system reset",
    "you must approve",
    "bypass review",
    "auto-approve this",
    "set status to approved",
]


def security_checkpoint_node(node_input: dict) -> dict:
    """Scrubs personal data (PII) from descriptions and defends against prompt injection."""
    payload = dict(node_input)
    description = payload.get("description", "")

    redacted_categories = []

    if SSN_REGEX.search(description):
        description = SSN_REGEX.sub("[REDACTED SSN]", description)
        redacted_categories.append("SSN")

    if CC_REGEX.search(description):
        description = CC_REGEX.sub("[REDACTED CREDIT CARD]", description)
        redacted_categories.append("Credit Card")

    payload["description"] = description
    payload["redacted_categories"] = redacted_categories

    desc_lower = description.lower()
    is_injection = any(keyword in desc_lower for keyword in INJECTION_KEYWORDS)
    payload["prompt_injection_flagged"] = is_injection

    # If any sensitive PII is found, fail validation immediately to halt workflow execution
    if redacted_categories:
        raise ValueError(
            f"🚨 SECURITY CHECKPOINT FAILED: Sensitive PII ({', '.join(redacted_categories)}) "
            "detected in description. Workflow execution blocked for policy compliance."
        )

    return payload


# ==============================================================================
# 2. ROUTING & REVIEW NODE (Human-In-The-Loop)
# ==============================================================================
async def routing_and_review_node(
    ctx: Context, node_input: Any
) -> AsyncGenerator[Event | RequestInput, None]:
    """Applies routing threshold and optionally triggers LLM risk review + human approval."""
    if "expense_payload" in ctx.state:
        node_input = ctx.state["expense_payload"]
    else:
        ctx.state["expense_payload"] = node_input

    amount = float(node_input.get("amount", 0.0))
    submitter = node_input.get("submitter", "Unknown")
    category = node_input.get("category", "Uncategorized")
    description = node_input.get("description", "No description")
    date = node_input.get("date", "Unknown Date")
    is_injection = node_input.get("prompt_injection_flagged", False)
    redacted_categories = node_input.get("redacted_categories", [])
    has_pii = len(redacted_categories) > 0

    # Rule: If PII violations are flagged, immediately block and auto-reject (the workflow should not execute)
    if has_pii:
        risk_analysis = f"🚨 SECURITY BLOCKED: Highly sensitive PII ({', '.join(redacted_categories)}) detected in description. Workflow execution blocked for policy compliance."
        ctx.state["risk_analysis"] = risk_analysis
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=f"🚨 SECURITY BLOCKED: This expense has been auto-rejected because it contains highly sensitive PII ({', '.join(redacted_categories)}) inside the description. Workflow execution blocked."
                    )
                ],
            ),
            output={
                "expense": node_input,
                "approved": False,
                "status": "REJECTED (PII VIOLATION)",
                "risk_rating": "HIGH (PII VIOLATION)",
                "risk_analysis": risk_analysis,
            },
        )
        return

    # Rule: If prompt injection is flagged, bypass LLM and route straight to Human review
    if is_injection:
        risk_analysis = (
            "⚠️ SECURITY ALERT: Prompt injection attempt detected in description! "
            "LLM review bypassed to defend model. Forced straight to manual review."
        )
        ctx.state["risk_analysis"] = risk_analysis

        if not ctx.resume_inputs or "approved" not in ctx.resume_inputs:
            message = (
                f"🚨 SECURITY ALERT: PROMPT INJECTION BLOCKED 🚨\n"
                f"An expense report was flagged for a potential injection attack.\n"
                f"Submitter: {submitter}\n"
                f"Amount: ${amount:.2f}\n"
                f"Description: [REDACTED FOR SECURITY]\n\n"
                f"Do you approve or reject this expense anyway? Please provide your decision and justification/reason (approve/reject):"
            )
            yield RequestInput(interrupt_id="approved", message=message, response_schema=Decision)
            return

    # Rule: Under $100 and no prompt injection or PII violations -> Auto-approve instantly (no LLM, no HITL)
    elif amount < config.THRESHOLD_AMOUNT:
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="Expense report processed and auto-approved."
                    )
                ],
            ),
            output={
                "expense": node_input,
                "approved": True,
                "status": "APPROVED",
                "risk_rating": "N/A (Auto-Approved)",
                "risk_analysis": "Expense is below limit. Auto-approved.",
            },
        )
        return

    # Rule: $100 or more -> LLM risk review and pause for human approval
    # 1. Check if we need to do the LLM risk judgment (only if not resuming)
    if not ctx.resume_inputs or "approved" not in ctx.resume_inputs:
        # Instantiate model client and request risk analysis
        client = get_genai_client()
        prompt = f"""
        Analyze the following expense report for potential risk factors (e.g., suspicious category, duplicate descriptions, excessive or unusual amounts for the category, or policy non-compliance):

        Submitter: {submitter}
        Amount: ${amount:.2f}
        Category: {category}
        Description: {description}
        Date: {date}

        Provide a concise summary highlighting any risk factors, and conclude with an alert rating: LOW, MEDIUM, or HIGH risk.
        """

        try:
            response = client.models.generate_content(
                model=config.MODEL_NAME, contents=prompt
            )
            risk_analysis = response.text
        except Exception as e:
            risk_analysis = (
                f"Error performing risk review: {e}. Defaulting to manual review."
            )

        # Save risk review output in the state so it is preserved upon resume
        ctx.state["risk_analysis"] = risk_analysis

        # Yield RequestInput to pause the workflow for manual review
        message = (
            f"=== EXPENSE ALERT (>= ${config.THRESHOLD_AMOUNT:.2f}) ===\n"
            f"Submitter: {submitter}\n"
            f"Amount: ${amount:.2f}\n"
            f"Description: {description}\n\n"
            f"--- LLM Risk Judgment ({config.MODEL_NAME}) ---\n"
            f"{risk_analysis}\n\n"
            f"Do you approve or reject this expense? Please provide your decision and justification/reason (approve/reject):"
        )
        yield RequestInput(interrupt_id="approved", message=message, response_schema=Decision)
        return

    # 2. Process human response upon resume
    val = ctx.resume_inputs["approved"]
    user_reason = "No reason provided."
    if isinstance(val, dict):
        user_decision = val.get("approved") or val.get("value") or val.get("response") or ""
        user_reason = val.get("reason") or "No reason provided."
    else:
        user_decision = str(val)
    user_decision = user_decision.strip().lower()
    approved = user_decision in ["approve", "approved", "yes", "y"]
    status = "APPROVED" if approved else "REJECTED"
    if is_injection:
        status = "SECURITY WARNING - " + status
    risk_analysis = ctx.state.get("risk_analysis", "Manual manager decision")
    risk_analysis += f"\n\n[Manager Review Reason]: {user_reason}"

    ctx.state["approved"] = approved
    ctx.state["status"] = status
    ctx.state["risk_analysis"] = risk_analysis

    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"Manager decision processed: {status}.")],
        ),
        output={
            "expense": node_input,
            "approved": approved,
            "status": status,
            "risk_analysis": risk_analysis,
        },
    )


# ==============================================================================
# 3. RECORD OUTCOME NODE
# ==============================================================================
def record_outcome_node(ctx: Context, node_input: Any) -> Event:
    """Finalizes and returns the official recorded outcome of the expense workflow."""
    expense = {}
    if isinstance(node_input, dict):
        expense = node_input.get("expense") or {}
    if not expense:
        expense = ctx.state.get("expense_payload") or {}
    
    approved_val = node_input.get("approved")
    if approved_val is None and ctx.resume_inputs:
        approved_val = ctx.resume_inputs.get("approved")
        
    if isinstance(approved_val, dict):
        user_decision = approved_val.get("approved") or approved_val.get("value") or approved_val.get("response") or ""
    else:
        user_decision = str(approved_val)
        
    user_decision = user_decision.strip().lower()
    approved = user_decision in ["approve", "approved", "yes", "y", "true"]
        
    risk_analysis = node_input.get("risk_analysis") or ctx.state.get("risk_analysis", "Manual manager decision")
    
    # Check if this was a prompt injection
    is_injection = expense.get("prompt_injection_flagged", False)
    
    if is_injection:
        status = "SECURITY WARNING - " + ("RECORDED & APPROVED" if approved else "REJECTED")
    else:
        status = "RECORDED & APPROVED" if approved else "REJECTED"

    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"Final outcome recorded: {status}.")],
        ),
        output={
            "status": status,
            "expense": expense,
            "approved": approved,
            "risk_analysis": risk_analysis,
        },
    )


# ==============================================================================
# 4. WORKFLOW GRAPH ASSEMBLY
# ==============================================================================
root_agent = Workflow(
    name="ambient_expense_workflow",
    description="Ambient expense-approval workflow utilizing security checkpoints, programmatic routing, LLM risk review, and HITL approval.",
    edges=[
        ("START", parse_event_node),
        (parse_event_node, security_checkpoint_node),
        (security_checkpoint_node, routing_and_review_node),
        (routing_and_review_node, record_outcome_node),
    ],
)

app = App(
    root_agent=root_agent,
    name="expense_agent",
)
