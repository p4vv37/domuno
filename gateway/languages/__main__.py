"""Validate the router language code for the shell entry points."""
import argparse

from . import language_codes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('AGENT_LANGUAGE', choices=language_codes())
    parser.parse_args()


if __name__ == '__main__':
    main()
