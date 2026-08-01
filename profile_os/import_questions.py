"""Import extracted Sundog/Marrek question banks into LT Rita.

Usage:
  python -m profile_os.import_questions --data-dir /app/data bank1.json bank2.json
"""

from __future__ import annotations

import argparse
import json

from .dynstores import DynamicStores
from .questions import QuestionPractice, load_question_files
from .storage import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        practice = QuestionPractice(store, DynamicStores(store))
        records = load_question_files(args.paths)
        print(json.dumps(practice.import_records(records), indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
