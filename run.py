"""Entry point for the Obsidian MCP Server."""

if __name__ == "__main__":
    from dotenv import load_dotenv

    # Load .env before server constructs its application from validated config.
    load_dotenv()

    from server import mcp

    mcp.run(transport="streamable-http")
