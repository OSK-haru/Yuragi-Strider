/*
 * ブラウザのスクリプト実行に組み込む SDK と、最初に置くファイル。
 * 手元のPython用と同じ attack_client の .py をそのまま使う（写しを持たない）。
 */

const sources = import.meta.glob('../../../attack_client/*.py', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const sample = import.meta.glob('../../../examples/reset_leakage_attack.py', { query: '?raw', import: 'default', eager: true }) as Record<string, string>

export const ATTACK_CLIENT_SDK: Record<string, string> = Object.fromEntries(Object.entries(sources)
  .map(([path, text]) => [`attack_client/${path.split('/').pop()}`, text]))

export const STARTER_FILES: Record<string, string> = {
  'main.py': [
    '# ターミナルで  python main.py  と打つと実行する。Ctrl+C で止まる。',
    '# 手元のPythonと同じ attack_client を使う。ここでは接続情報を省略できる。',
    'from attack_client import AttackSession, Circuit',
    '',
    'with AttackSession() as session:',
    '    job = session.run(Circuit(1).measure(0, 0), shots=32)',
    '    result = job.result()',
    '    print(result.counts, result.probability_of_one(0))',
    '',
  ].join('\n'),
  ...Object.fromEntries(Object.entries(sample).map(([path, text]) => [path.split('/').pop() as string, text])),
}
