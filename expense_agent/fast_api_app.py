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
import os
import json
import logging
from typing import Any

from fastapi import FastAPI
from google.adk.apps import App as ADKApp
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import InMemoryRunner
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

from expense_agent.agent import root_agent
from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

# 1. Standard Python Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

setup_telemetry()

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
# Shared SQLite DB path with the Playground
db_path = os.path.join(AGENT_DIR, ".adk", "session.db")
os.makedirs(os.path.dirname(db_path), exist_ok=True)
session_service = SqliteSessionService(db_path=db_path)

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=None,  # Forces ADK to use the correct SqliteSessionService internally
    otel_to_cloud=False,  # Set otel_to_cloud to False as requested
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"

# 2. ADK Runner for Programmatic Processing of Pub/Sub Events
app_instance = ADKApp(name="expense_agent", root_agent=root_agent)
runner = InMemoryRunner(app=app_instance)
runner.session_service = session_service


@app.post("/")
@app.post("/pubsub")
async def handle_pubsub(payload: dict[str, Any]) -> dict[str, Any]:
    """Pub/Sub push subscription endpoint.
    
    Accepts fully-qualified subscription paths and normalizes them down
    to a short name to keep session records highly readable.
    """
    subscription_path = payload.get("subscription", "projects/default/subscriptions/ambient-expense-sub")
    
    # Normalize subscription: "projects/my-project/subscriptions/my-sub" -> "my-sub"
    subscription_short_name = subscription_path.split("/")[-1]
    
    logger.info(f"Received Pub/Sub message from subscription: {subscription_short_name}")
    
    session_id = subscription_short_name
    
    # Retrieve or create session
    try:
        session = await runner.session_service.get_session(
            app_name="expense_agent", user_id="user", session_id=session_id
        )
    except Exception:
        session = None
        
    if session is None:
        session = await runner.session_service.create_session(
            app_name="expense_agent", user_id="user", session_id=session_id
        )
        
    events = []
    async for event in runner.run_async(
        user_id="user",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(text=json.dumps(payload))]
        )
    ):
        events.append(event)
        
    if events:
        final_event = events[-1]
        logger.info(f"Inference completed for session {session_id}. Interrupted: {final_event.interrupted}")
        return {
            "status": "success",
            "session_id": session_id,
            "output": final_event.output,
            "interrupted": final_event.interrupted,
        }
        
    return {"status": "no_events", "session_id": session_id}


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9500)
