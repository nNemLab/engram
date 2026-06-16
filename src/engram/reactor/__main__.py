"""Entry point: engram-reactor"""
import logging

from .reactor import install_signal_handlers, run


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    install_signal_handlers()
    run()


if __name__ == "__main__":
    main()
