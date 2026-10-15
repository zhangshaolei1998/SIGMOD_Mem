#!/usr/bin/env python3
"""Recompute archived answer-score summaries without re-scoring answers."""
import argparse
import collections
import json
import math
import statistics
import zipfile
from pathlib import Path


def audit(archive):
    output = []
    with zipfile.ZipFile(archive) as bundle:
        for name in sorted(bundle.namelist()):
            if not name.endswith('/records.jsonl'):
                continue
            rows = [json.loads(line) for line in
                    bundle.read(name).decode('utf-8').splitlines() if line.strip()]
            if not rows:
                raise ValueError(f'{name}: empty record set')
            ids = [row['id'] for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f'{name}: duplicate identifiers')
            for row in rows:
                for field in ('memcatalog_score', 'full_context_score'):
                    if field not in row:
                        continue
                    score = row[field]
                    if not isinstance(score, (int, float)) or not math.isfinite(score):
                        raise ValueError(f'{name}: invalid {field}')
                    if not 0 <= score <= 1:
                        raise ValueError(f'{name}: {field} outside [0, 1]')
            paired = all('full_context_score' in row for row in rows)
            if any('full_context_score' in row for row in rows) and not paired:
                raise ValueError(f'{name}: partially populated paired scores')
            item = {
                'records': name,
                'n': len(rows),
                'prototype_mean': 100 * statistics.mean(
                    row['memcatalog_score'] for row in rows),
                'empty_answers': sum(not row['memcatalog_answer'].strip()
                                     for row in rows),
                'categories': {},
            }
            if paired:
                differences = [row['memcatalog_score'] - row['full_context_score']
                               for row in rows]
                item.update({
                    'full_context_mean': 100 * statistics.mean(
                        row['full_context_score'] for row in rows),
                    'paired_difference': 100 * statistics.mean(differences),
                    'wins': sum(value > 1e-12 for value in differences),
                    'ties': sum(abs(value) <= 1e-12 for value in differences),
                    'losses': sum(value < -1e-12 for value in differences),
                })
            categories = collections.defaultdict(list)
            for row in rows:
                categories[row['category']].append(row)
            for key, group in sorted(categories.items()):
                category = {
                    'n': len(group),
                    'prototype_mean': 100 * statistics.mean(
                        row['memcatalog_score'] for row in group),
                }
                if paired:
                    category['full_context_mean'] = 100 * statistics.mean(
                        row['full_context_score'] for row in group)
                item['categories'][key] = category
            output.append(item)
    return {'total_records': sum(item['n'] for item in output), 'runs': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='?', type=Path,
                        default=Path(__file__).resolve().parents[1] / '2.zip')
    arguments = parser.parse_args()
    print(json.dumps(audit(arguments.archive), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
