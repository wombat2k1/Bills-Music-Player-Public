"""Application entry point."""
def main() -> int:
    from Main import main as launcher_main

    return launcher_main()


if __name__ == "__main__":
    raise SystemExit(main())

