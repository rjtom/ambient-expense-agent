.PHONY: install playground clean test

install:
	@echo "Installing project dependencies using uv..."
	uv sync

playground:
	@echo "Checking if port 8085 is in use..."
	@lsof -i :8085 >/dev/null && (echo "Error: Port 8085 is already in use! Please stop the existing process or use another port." && exit 1) || true
	@echo "Starting ADK 2.0 playground in background on port 8085..."
	uv run agents-cli playground --port 8085 > playground.log 2>&1 &
	@echo "Waiting for playground to initialize..."
	@sleep 3
	@echo "Playground started! Logs are being written to 'playground.log'."
	@echo "Opening http://localhost:8085 in browser..."
	@open http://localhost:8085 || echo "Please visit http://localhost:8085 in your browser."

test:
	@echo "Running all integration and unit tests..."
	uv run pytest

run:
	@echo "Starting the ambient web service on port 9500..."
	uv run python -m expense_agent.fast_api_app

run-bg:
	@echo "Checking if port 9500 is in use..."
	@lsof -i :9500 >/dev/null && (echo "Error: Port 9500 is already in use!" && exit 1) || true
	@echo "Starting ambient web service in background on port 9500..."
	uv run python -m expense_agent.fast_api_app > server.log 2>&1 &
	@echo "Web service started in the background. Logs are written to server.log"

clean:
	@echo "Cleaning up temp files and logs..."
	rm -f playground.log server.log

generate-traces:
	@echo "Generating offline traces using basic-dataset.json..."
	uv run python tests/eval/generate_traces.py

grade:
	@echo "Grading generated traces using custom LLM-as-judge metrics..."
	uv run agents-cli eval grade --config tests/eval/eval_config.yaml --traces artifacts/traces/generated_traces.json

