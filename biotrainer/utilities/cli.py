import argparse
import logging

from .executer import parse_config_file_and_execute_run


def headless_main(config_file_path: str):
    parse_config_file_and_execute_run(config_file_path=config_file_path)


def main(args=None):
    """
    Pipeline commandline entry point
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Jax likes to print warnings
    logging.captureWarnings(True)

    parser = argparse.ArgumentParser(description='Trains models on protein embeddings.')
    parser.add_argument('config_path', metavar='/path/to/pipeline_definition.yml', type=str, nargs=1,
                        help='The path to the config. For examples, see folder "parameter examples".')
    arguments = parser.parse_args()

    parse_config_file_and_execute_run(arguments.config_path[0])


if __name__ == '__main__':
    main()
