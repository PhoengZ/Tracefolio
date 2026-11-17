import argparse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tracefolio")
    parser.add_argument("--version", action="version", version="tracefolio 0.1.0")
    parser.parse_args(argv)
    return 0

