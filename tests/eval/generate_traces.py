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

import asyncio
import json
import os
import sys
from pathlib import Path
from vertexai import types as vertex_types
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from expense_agent.agent import root_agent

# Ensure artifacts directory exists
os.makedirs("artifacts/traces", exist_ok=True)

def serialize_content(content):
    if not content:
        return None
    
    parts_list = []
    for part in content.parts:
        part_dict = {}
        if part.text:
            part_dict["text"] = part.text
        elif part.function_call:
            part_dict["function_call"] = {
                "name": part.function_call.name,
                "args": part.function_call.args
            }
        elif part.function_response:
            part_dict["function_response"] = {
                "name": part.function_response.name,
                "response": part.function_response.response
            }
        if part_dict:
            parts_list.append(part_dict)
            
    return {
        "role": content.role or "model",
        "parts": parts_list
    }

async def run_case(case):
    case_id = case.get("eval_case_id")
    prompt_text = case["prompt"]["parts"][0]["text"]
    
    app_instance = App(name="expense_agent", root_agent=root_agent)
    runner = InMemoryRunner(app=app_instance)
    
    session = await runner.session_service.create_session(
        app_name="expense_agent", user_id="eval_user"
    )
    
    turns = []
    events_turn0 = []
    
    # 1. Add user prompt event to Turn 0
    events_turn0.append({
        "author": "user",
        "content": {
            "role": "user",
            "parts": [{"text": prompt_text}]
        }
    })
    
    # Run first turn
    interrupted_event = None
    async for event in runner.run_async(
        user_id="eval_user",
        session_id=session.id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part.from_text(text=prompt_text)]
        ),
    ):
        if hasattr(event, "content") and event.content:
            serialized = serialize_content(event.content)
            if serialized:
                events_turn0.append({
                    "author": "expense_agent",
                    "content": serialized
                })
            
            # Check for interruption
            for part in event.content.parts:
                if hasattr(part, "function_call") and part.function_call and part.function_call.name == "adk_request_input":
                    interrupted_event = event

    turns.append({
        "turn_index": 0,
        "turn_id": "turn_0",
        "events": events_turn0
    })
    
    # 2. Handle programmatic resumption if interrupted
    if interrupted_event:
        # Determine programmatic decision
        decision = "approve"
        if "rejection" in case_id or "escalation" in case_id:
            decision = "reject"
            
        print(f"[{case_id}] HITL interrupted. Automating decision: {decision}")
        
        # User response event in Turn 1
        events_turn1 = [{
            "author": "user",
            "content": {
                "role": "user",
                "parts": [{
                    "function_response": {
                        "name": "adk_request_input",
                        "id": "approved",
                        "response": {"approved": decision}
                    }
                }]
            }
        }]
        
        # Resume run
        async for event in runner.run_async(
            user_id="eval_user",
            session_id=session.id,
            new_message=genai_types.Content(
                role="user",
                parts=[
                    genai_types.Part(
                        function_response=genai_types.FunctionResponse(
                            name="adk_request_input",
                            id="approved",
                            response={"approved": decision},
                        )
                    )
                ],
            ),
        ):
            if hasattr(event, "content") and event.content:
                serialized = serialize_content(event.content)
                if serialized:
                    events_turn1.append({
                        "author": "expense_agent",
                        "content": serialized
                    })
                    
        turns.append({
            "turn_index": 1,
            "turn_id": "turn_1",
            "events": events_turn1
        })

    # Extract final text response for the 'responses' field
    final_text = "No final response text"
    for turn in reversed(turns):
        for event in reversed(turn["events"]):
            if event["author"] == "expense_agent" and "content" in event:
                for part in event["content"]["parts"]:
                    if "text" in part:
                        final_text = part["text"]
                        break
                if final_text != "No final response text":
                    break
        if final_text != "No final response text":
            break

    # Build the full EvalCase dict
    eval_case_dict = {
        "eval_case_id": case_id,
        "prompt": {
            "role": "user",
            "parts": [{"text": prompt_text}]
        },
        "agent_data": {
            "agents": {
                "expense_agent": {
                    "agent_id": "expense_agent",
                    "instruction": root_agent.description or "Ambient expense-approval agent"
                }
            },
            "turns": turns
        },
        "responses": [
            {
                "response": {
                    "role": "model",
                    "parts": [{"text": final_text}]
                }
            }
        ]
    }
    
    return eval_case_dict

async def main():
    dataset_path = Path("tests/eval/datasets/basic-dataset.json")
    output_path = Path("artifacts/traces/generated_traces.json")
    
    print(f"Loading dataset from {dataset_path}...")
    with open(dataset_path, encoding="utf-8") as f:
        dataset = json.load(f)
        
    eval_cases = dataset.get("eval_cases", [])
    print(f"Loaded {len(eval_cases)} eval case(s). Running inference...")
    
    merged_cases = []
    for case in eval_cases:
        case_id = case.get("eval_case_id")
        print(f"Running scenario: {case_id}...")
        try:
            merged_case_dict = await run_case(case)
            # Validate using vertexai types to be extra safe and correct
            validated = vertex_types.EvalCase.model_validate(merged_case_dict)
            merged_cases.append(validated)
            print(f"Scenario {case_id} completed successfully.")
        except Exception as exc:
            print(f"Scenario {case_id} FAILED: {exc}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
            
    result = vertex_types.EvaluationDataset(eval_cases=merged_cases)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        result.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    print(f"Saved populated traces to {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
