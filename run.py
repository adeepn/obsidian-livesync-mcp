"""Entry point for the Obsidian MCP Server."""

from dotenv import load_dotenv

# Load .env before server constructs its application from validated config.
load_dotenv()

from server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
