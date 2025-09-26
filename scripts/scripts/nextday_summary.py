#!/usr/bin/env python3
import os
import sys
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pairs",
        type=str,
        default=os.getenv("PAIRS", "EURUSD,USDJPY"),
        help="Seznam měnových párů oddělený čárkou"
    )
    args = parser.parse_args()

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("Nebyl zadán žádný pár.")
        sys.exit(2)  # kód 2 = 'žádná data', workflow to vezme jako OK

    print("Zpracovávám páry:", pairs)

    # TODO: sem přijde tvoje logika pro stažení fundamentálních zpráv
    # Aktuálně jen simulace -> vrátíme 2 = žádná data
    sys.exit(2)

if __name__ == "__main__":
    main()
