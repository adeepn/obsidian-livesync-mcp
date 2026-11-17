"""Entry point for the Obsidian MCP Server."""

from dotenv import load_dotenv

# Load .env before importing server: server.py reads os.environ at import
# time (audit A2), so environment expansion must happen first.
load_dotenv()

from server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
