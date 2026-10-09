"""递归转换 Parquet，保留字段、行序和相对于源目录的文件结构。"""

import argparse
import json
from pathlib import Path

import pyarrow.parquet as pq


def convert_tree(source, output=None):
    source = Path(source).resolve()
    output = Path(output).resolve() if output is not None else source
    if not source.is_dir():
        raise NotADirectoryError(source)

    files = sorted(path for path in source.rglob("*")
                   if path.is_file() and path.suffix.lower() == ".parquet")
    total_rows = 0
    for path in files:
        target = output / path.relative_to(source).with_suffix(".jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with target.open("w", encoding="utf-8", newline="\n") as stream:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
                for record in batch.to_pylist():
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    rows += 1
        total_rows += rows
        print(f"{path} -> {target} ({rows} rows)")

    print(f"Converted {len(files)} files, {total_rows} rows.")
    return len(files), total_rows


def main():
    parser = argparse.ArgumentParser(
        description="递归将 Parquet 转成同名 JSONL，默认保存在源文件旁。"
    )
    parser.add_argument("source", type=Path, help="要遍历的数据集目录")
    parser.add_argument("-o", "--output", type=Path,
                        help="输出根目录；省略时使用源目录，保留相对路径")
    args = parser.parse_args()
    convert_tree(args.source, args.output)


if __name__ == "__main__":
    main()
