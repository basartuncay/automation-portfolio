import logging

from .worker import Config, run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
run_forever(Config.from_env())
