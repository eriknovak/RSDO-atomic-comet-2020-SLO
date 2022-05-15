import os
import re
import json
import argparse
import pandas as pd
from nltk.translate.bleu_score import sentence_bleu
from utils.utils import read_jsonl, write_jsonl
from evaluation.eval import QGEvalCap
from tabulate import tabulate

from collections import defaultdict


def retrieve_ref_data(test_file_path):
    df = pd.read_csv(test_file_path, encoding="utf-8", sep="\t")
    return [
        {
            "head": row.Index[0],
            "relation": row.Index[1],
            "references": list(filter(lambda x: not pd.isnull(x), row.tail_event)),
        }
        for row in df.groupby(["head_event", "relation"]).agg(list).itertuples()
    ]


def get_reference_sentences(ref_data):
    return [ref["references"] for ref in ref_data]


def postprocess(sentence):
    return sentence


def get_heads_and_relations(ref_data):
    return [{"head": ref["head"], "relation": ref["relation"]} for ref in ref_data]


def get_hypothesises(filename):
    result = []
    import json

    with open(filename) as file:
        for line in file:
            result.append(
                [
                    re.sub(r"[PAD]", "", gen.split("[GEN]")[1]).strip()
                    for gen in json.loads(line)["generations"]
                ]
            )
    return result


def preprocess_generations(args):
    test_file_path = args.test_file_path
    pred_file_path = args.pred_file_path

    outfile_path = os.path.join(
        os.path.dirname(pred_file_path),
        os.path.basename(pred_file_path).split(".")[0] + "_gens.jsonl",
    )

    outfile = open(outfile_path, "w")

    ref_data = retrieve_ref_data(test_file_path)
    references_list = get_reference_sentences(ref_data)
    heads_relations = get_heads_and_relations(ref_data)
    hypothesises = get_hypothesises(pred_file_path)

    total_bleu_1 = 0
    total_bleu_2 = 0
    total_bleu_3 = 0
    total_bleu_4 = 0

    relation_bleu_1 = defaultdict(lambda: defaultdict(int))

    count = 0

    for head_relation, references, hypothesis in zip(
        heads_relations, references_list, hypothesises
    ):
        bleu_1 = sentence_bleu(references, hypothesis, weights=[1.0])
        bleu_2 = sentence_bleu(references, hypothesis, weights=[0.5, 0.5])
        bleu_3 = sentence_bleu(references, hypothesis, weights=[0.34, 0.33, 0.33])
        bleu_4 = sentence_bleu(references, hypothesis)

        result = {
            "generation": postprocess(hypothesis),
            "references": [postprocess(reference) for reference in references],
            "input": head_relation,
        }
        if hypothesis != "none":
            total_bleu_1 += bleu_1
            total_bleu_2 += bleu_2
            total_bleu_3 += bleu_3
            total_bleu_4 += bleu_4

            relation_bleu_1[head_relation["relation"]]["total"] += bleu_1
            relation_bleu_1[head_relation["relation"]]["count"] += 1

            count += 1

        outfile.write(json.dumps(result) + "\n")
    print("gens non-none", count)
    outfile_scores = open(
        os.path.join(
            os.path.dirname(pred_file_path),
            os.path.basename(pred_file_path).split(".")[0] + "_scores.jsonl",
        ),
        "w",
    )

    summary = {
        "bleu1": total_bleu_1 / count,
        "bleu2": total_bleu_2 / count,
        "bleu3": total_bleu_3 / count,
        "bleu4": total_bleu_4 / count,
    }

    for relation in relation_bleu_1:
        summary[relation] = (
            relation_bleu_1[relation]["total"] / relation_bleu_1[relation]["count"]
        )

    outfile_scores.write(json.dumps(summary) + "\n")
    excel_str = ""
    for key in summary:
        excel_str += str(key) + "\t"
    outfile_scores.write(excel_str.strip())
    outfile_scores.write("\n")
    excel_str = ""
    for key in summary:
        excel_str += str(summary[key]) + "\t"

    outfile_scores.write(excel_str.strip())

    print(f"Saved gens in {outfile_path}")

    return os.path.abspath(outfile_path)


def get_tuple(l):
    gens = [l["generation"]]
    head = l["input"]["head"]
    tails = l["references"]
    relation = l["input"]["relation"]
    return {"head": head, "relation": relation, "tails": tails, "generations": gens}


def get2(l):
    return list(zip(*l))[1]


def topk_eval(model_name, data, k):

    topk_gts = {}
    topk_res = {}
    instances = []
    topk_exact_match = []
    topk_exact_match_not_none = []
    topk_bleu_score = []

    topk_is_head = []

    for i, l in enumerate(data):
        t = get_tuple(l)
        gens = t["generations"]
        tails = t["tails"]
        head = t["head"]

        for (j, g) in enumerate(gens[:k]):

            instance = t.copy()
            instance["generation"] = g
            instances.append(instance)

            key = str(i) + "_" + str(j)
            topk_gts[key] = tails
            topk_res[key] = [g]

            if g in tails:
                topk_exact_match.append((l, 1))
                if g != "none":
                    topk_exact_match_not_none.append((l, 1))
            else:
                topk_exact_match.append((l, 0))
                if g != "none":
                    topk_exact_match_not_none.append((l, 0))
            if g == head:
                topk_is_head.append((l, 1))
            else:
                topk_is_head.append((l, 0))

    QGEval = QGEvalCap(model_name, topk_gts, topk_res)
    score, scores = QGEval.evaluate()

    return score, scores, instances


def eval(data_file, model_name):

    data = read_jsonl(data_file)

    if len(data) == 0:
        return None

    return topk_eval(model_name, data, k=1)


def toRow(name, results, columns):
    return [name] + [format(float(results[c]), "#.3f") for c in columns]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test_file_path", type=str, help="The test file path")
    parser.add_argument("--pred_file_path", type=str, help="The prediction file path")
    args = parser.parse_args()

    generations_file = preprocess_generations(args)

    input_file = generations_file

    expts = [[input_file, os.path.basename(input_file).split(".")[0]]]

    scores_per_model = []
    add_column = True
    for f, m in expts:
        result_file = "./results/{}_scores.jsonl".format(m)

        s, scores, instances = eval(f, model_name=m)
        if s == None:
            print("Skipping ", m)
            continue

        for k in scores.keys():
            assert len(scores[k]) == len(instances)

        results = {
            "model": m,
            "scores": s,
            "all_scores": scores,
            "instances": instances,
        }
        write_jsonl(result_file, [results])

        scores_per_model.append(results)
        columns = list(results["scores"].keys())
        s_row = toRow(results["model"], results["scores"], columns)
        if add_column:
            rows = [[""] + columns]
            add_column = False
        rows.append(s_row)

    import datetime

    date = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    print(scores_per_model)

    write_jsonl("./results/scores_{}.jsonl".format(date), scores_per_model)
    print(tabulate(rows, headers="firstrow", tablefmt="latex", floatfmt="#.3f"))
    print(tabulate(rows, tablefmt="tsv", floatfmt="#.3f"))


if __name__ == "__main__":
    main()