"""Reset漏洩の攻撃サンプル: 収集 → 学習 → 観測 → 推定 → 提出。

画面のターミナルで実行する（その画面の環境へ接続する）。

    python reset_leakage_attack.py [--answer]

この課題は交互観測（抽象コマンド）を使う。自由モードでは「抽象コマンド: 交互観測を有効にする」を
オンにした環境で動く。回路は測定だけにする（測定の前にゲートで状態を変えると、Resetの残りは
観測できない）。ここから、回路・回数・判定のしかたを自由に書き換えて試せる。
"""

from __future__ import annotations

import argparse

import numpy as np

from attack_client import AttackSession, Circuit


def collect(session: AttackSession, reference: str, runs: int, shots: int) -> np.ndarray:
    """同じ観測を runs 回、先に全部投げてから結果を集め、各回の1の割合を返す。"""
    circuit = Circuit(1).measure(0, 0)  # 何もせず測るだけ
    jobs = [session.run(circuit, shots=shots, interleave=reference) for _ in range(runs)]
    return np.array([job.result().probability_of_one(0) for job in jobs])


def fit_threshold(zero: np.ndarray, one: np.ndarray) -> tuple[float, str]:
    """2つのラベルの平均の中点をしきい値にし、高いほうを1と判定する向きを決める。"""
    low, high = float(zero.mean()), float(one.mean())
    if low == high:
        raise SystemExit("2つのラベルで1の割合が同じです。回路か shots を見直してください。")
    return (low + high) / 2.0, "ge" if high > low else "le"


def run_attack(session: AttackSession, *, runs: int = 3, shots: int = 32, submit: bool = False) -> dict:
    zero = collect(session, "ref:0", runs, shots)
    one = collect(session, "ref:1", runs, shots)
    threshold, direction = fit_threshold(zero, one)
    victim = collect(session, "other", 1, shots)[0]
    guess = int((victim >= threshold) == (direction == "ge"))
    report = {"ref0": zero.tolist(), "ref1": one.tolist(), "threshold": threshold,
              "direction": direction, "observed": float(victim), "guess": guess}
    if submit:
        report["submission"] = session.answer(round(threshold, 4), direction)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--answer", action="store_true", help="学習した規則をそのまま提出する（課題モード）")
    args = parser.parse_args()
    with AttackSession() as session:
        report = run_attack(session, runs=args.runs, submit=args.answer)
    print(f"参照ラベル0の1の割合: {np.round(report['ref0'], 3)}")
    print(f"参照ラベル1の1の割合: {np.round(report['ref1'], 3)}")
    print(f"判定規則: 1の割合が {report['threshold']:.4f} {'以上' if report['direction'] == 'ge' else '未満'}なら 1")
    print(f"観測した1の割合 {report['observed']:.3f} → 推定: {report['guess']}")
    if "submission" in report:
        print("提出結果:", report["submission"])


if __name__ == "__main__":
    main()
