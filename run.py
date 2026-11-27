"""Entry point for the Obsidian MCP Server."""

if __name__ == "__main__":
    import logging
    import sys

    from config import ConfigError
    from diagnostic_hygiene import cause_name

    try:
        from dotenv import load_dotenv

        # Load .env before server constructs its application from validated config.
        load_dotenv()
        from server import mcp, run_host
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as error:
        logging.getLogger(__name__).error(
            'application import failed; cause: %s', cause_name(type(error))
        )
        raise SystemExit(1) from None

    run_host(mcp.run)
