import argparse
import logging
import time

from app.service import Service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["document", "checklist", "scoring"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    service = Service()
    service.repo.initialize()
    while True:
        if not service.run_one(args.stage):
            time.sleep(1)


if __name__ == "__main__":
    main()
