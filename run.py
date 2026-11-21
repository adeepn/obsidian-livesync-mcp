"""Entry point for the Obsidian MCP Server."""

if __name__ == "__main__":
    from dotenv import load_dotenv

    # Load .env before server constructs its application from validated config.
    load_dotenv()

    import sys
    from config import ConfigError

    try:
        from server import mcp
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from None

    mcp.run(transport="streamable-http")
