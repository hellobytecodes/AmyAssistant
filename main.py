#!/usr/bin/env python3
"""
main.py
-------
Entry point for Amy. Run this after setting GEMINI_API_KEY (see config.py).

    python main.py
"""

import sys

import config


def check_setup() -> bool:
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "PASTE_YOUR_GEMINI_API_KEY_HERE":
        print(
            "\n[Amy] No Gemini API key set.\n"
            "Get a free key at https://aistudio.google.com/apikey, then open config.py\n"
            "and paste it into GEMINI_API_KEY = \"...\" near the top of the file.\n"
        )
        return False
    return True


def main() -> None:
    if not check_setup():
        sys.exit(1)

    from gui.main_window import launch

    launch()


if __name__ == "__main__":
    main()
