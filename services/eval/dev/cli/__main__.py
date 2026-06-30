"""Module entrypoint for the local-only dev CLI umbrella.

The dev CLIs ship no console_scripts (they import the ChromaDB stub and never go
into the app/ wheel), so the umbrella group is invoked as a module:

    python -m dev.cli check phoenix
    python -m dev.cli check bedrock
"""

from dev.cli import main

if __name__ == "__main__":
    main()
