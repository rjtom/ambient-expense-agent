import asyncio
import json
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types
from expense_agent.agent import root_agent

async def main():
    app = App(name="expense_agent", root_agent=root_agent)
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="expense_agent", user_id="test_user"
    )

    input_payload = '{"amount": 150.0, "submitter": "Bob", "category": "Meals", "description": "Client Dinner", "date": "2026-06-17"}'
    
    print("--- Turn 1 (HITL Interruption) ---")
    events = []
    async for event in runner.run_async(
        user_id="test_user",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=input_payload)]
        ),
    ):
        events.append(event)
        print(f"Event: {type(event).__name__}")
        print(f"  interrupted: {getattr(event, 'interrupted', None)}")
        print(f"  content: {event.content if hasattr(event, 'content') else None}")
        print(f"  output: {event.output if hasattr(event, 'output') else None}")
        print("---")

    last_event = events[-1]
    is_hitl = False
    if hasattr(last_event, "content") and last_event.content and last_event.content.parts:
        part = last_event.content.parts[0]
        if hasattr(part, "function_call") and part.function_call and part.function_call.name == "adk_request_input":
            is_hitl = True

    print(f"Is HITL: {is_hitl}")

    if is_hitl:
        print("\n--- Turn 2 (HITL Resume with Approve) ---")
        async for event in runner.run_async(
            user_id="test_user",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="adk_request_input",
                            id="approved",
                            response={"approved": "approve"},
                        )
                    )
                ],
            ),
        ):
            print(f"Event: {type(event).__name__}")
            print(f"  interrupted: {getattr(event, 'interrupted', None)}")
            print(f"  content: {event.content if hasattr(event, 'content') else None}")
            print(f"  output: {event.output if hasattr(event, 'output') else None}")
            print("---")

if __name__ == "__main__":
    asyncio.run(main())
