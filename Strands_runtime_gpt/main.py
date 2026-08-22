"""Amazon Bedrock AgentCore entry point for the Strands data analyst."""

import logging
from contextlib import asynccontextmanager

from bedrock_agentcore import BedrockAgentCoreApp

import agent
import skills_sync


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("strands_data_analyst")


@asynccontextmanager
async def lifespan(_app):
    """Load optional analysis skills before accepting invocations."""
    skills_sync.sync_skills()
    yield


app = BedrockAgentCoreApp(lifespan=lifespan)


@app.entrypoint
def invoke(payload, context):
    """Invoke the data analyst.

    The default response is the same simple JSON shape as the generated
    AgentCore template. Set ``stream: true`` to receive incremental SSE events.
    """
    request = agent.InvocationRequest.from_payload(payload, context)
    logger.info(
        "Invoke: turns=%d actor=%s session=%s stream=%s",
        len(request.messages),
        request.actor_id,
        request.session_id,
        request.stream,
    )
    if request.stream:
        return agent.stream(request)
    return agent.run(request)


if __name__ == "__main__":
    app.run()
