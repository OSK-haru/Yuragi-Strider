/*
 * 攻撃者ターミナルのコマンド解析。
 *
 * 攻撃者がサービスに対して行う操作（接続・回路送信・ジョブ照会・取消・待機）は、
 * ボタンではなくすべてこのターミナルへ打つコマンドで行う。使えるのは他の利用者と
 * 同じ公開サービスの操作だけで、攻撃者専用の特権コマンドは無い。
 * ここは文字列を解釈して命令に変えるだけで、実際の要求は呼び出し側が
 * 既存の workspace 経由で出す。
 */

import type { InterleaveReference, SolutionSubmission } from '../types/attackLive'
import { parseWaitDuration } from './attackLiveSettings'

export type TerminalCommand =
  | { kind: 'help' }
  | { kind: 'clear' }
  | { kind: 'check' }
  | { kind: 'connect' }
  | { kind: 'pending' }
  | { kind: 'submit'; shots?: number; initialization?: 'prepare' | 'inherit'; interleave?: InterleaveReference; measure?: Record<number, number> }
  | { kind: 'job'; action: 'status' | 'result' | 'cancel' | 'activity'; jobId?: string }
  | { kind: 'answer'; submission: SolutionSubmission }
  | { kind: 'wait'; target: 'any' }
  | { kind: 'wait'; target: 'duration'; durationUs: number }
  | { kind: 'wait'; target: 'request'; requestId: string }
  | { kind: 'python'; file: string; argv: string[] }
  | { kind: 'error'; message: string }

export const TERMINAL_HELP = [
  'connect                                       サービスへ接続（切断時）',
  'submit [--shots N] [--init prepare|inherit] [--interleave other|ref:0|ref:1] [--measure qN=cM,...]',
  '                                              編集中の回路を送信（測定先の省略分は qN → cN）',
  '                                              --interleave は抽象コマンド（現実のサービスに無い操作）。環境設定で有効にした環境だけで使える',
  'answer --threshold X --direction ge|le       判定規則を提出（課題モードのみ・回数制限あり）',
  'answer --message <文字列>                     メッセージを提出（メッセージの課題のみ・回数制限あり）',
  '                                              X は1の割合（0〜1）。ge: 割合がX以上なら1と判定、le: 割合がX未満なら1と判定',
  'status [ジョブID]                             ジョブの状態を照会',
  'result [ジョブID]                             ジョブの結果を照会',
  'cancel [ジョブID]                             ジョブを取消',
  'activity [ジョブID]                           被害者の動きを照会（抽象コマンド。自由モードで有効にした環境だけ）',
  'wait <時間>                                   指定時間だけ待機（例: wait 250us）',
  'wait any                                      最も早いreplyを待つ',
  'wait <要求ID>                                 指定した要求のreplyを待つ',
  'pending                                       未完了の要求を一覧',
  'python <ファイル> [引数...]                   スクリプトのファイルを実行（Ctrl+C で停止。実行中に打った行は input() へ渡る）',
  'check                                         送信前検査をやり直す',
  'clear                                         表示を消去',
  'help                                          この一覧',
]

const JOB_ID = /^[\x21-\x7e]{1,128}$/
const MEASURE_PAIR = /^q?(\d{1,3})[=:]c?(\d{1,3})$/
const MAX_QUBIT_INDEX = 255
/* サーバーが発行する要求IDは uuid4().hex。待機時間（250us など）とは形式で区別できる。 */
const REQUEST_ID = /^[a-f0-9]{32}$/

/** 空行は null。解釈できない入力は kind: 'error' で理由を返す。 */
export function parseTerminalCommand(line: string): TerminalCommand | null {
  const tokens = line.trim().split(/\s+/).filter(Boolean)
  if (!tokens.length) return null
  const [name, ...args] = tokens
  switch (name) {
    case 'help':
    case 'clear':
    case 'check':
    case 'connect':
    case 'pending':
      return args.length ? { kind: 'error', message: `${name} は引数を取りません。` } : { kind: name }
    case 'submit':
      return parseSubmit(args)
    case 'python':
      if (!args.length) return { kind: 'error', message: 'python <ファイル> の形で、実行するファイルを指定してください（例: python main.py）。' }
      return { kind: 'python', file: args[0], argv: args.slice(1) }
    case 'answer':
      return parseAnswer(args)
    case 'status':
    case 'result':
    case 'activity':
    case 'cancel': {
      if (args.length > 1) return { kind: 'error', message: `${name} に渡せるジョブIDは1つです。` }
      if (args.length === 1 && !JOB_ID.test(args[0])) return { kind: 'error', message: 'ジョブIDの形式が正しくありません。' }
      return { kind: 'job', action: name, ...(args.length ? { jobId: args[0] } : {}) }
    }
    case 'wait': {
      if (args.length !== 1) return { kind: 'error', message: 'wait <時間>、wait any、wait <要求ID> のいずれかで指定してください。' }
      if (args[0] === 'any') return { kind: 'wait', target: 'any' }
      if (REQUEST_ID.test(args[0])) return { kind: 'wait', target: 'request', requestId: args[0] }
      try {
        return { kind: 'wait', target: 'duration', durationUs: parseWaitDuration(args[0]) }
      } catch (error) {
        return { kind: 'error', message: error instanceof Error ? error.message : '待機時間を確認してください。' }
      }
    }
    default:
      return { kind: 'error', message: `不明なコマンドです: ${name}（help で一覧）` }
  }
}

function parseAnswer(args: string[]): TerminalCommand {
  if (args[0] === '--message') {
    /* 残りの語をそのまま1つの文字列にする（連続した空白は1つになる）。 */
    const message = args.slice(1).join(' ')
    if (!message) return { kind: 'error', message: 'answer --message <文字列> の形で、提出するメッセージを指定してください。' }
    return { kind: 'answer', submission: { message } }
  }
  let threshold: number | undefined
  let direction: 'ge' | 'le' | undefined
  for (let index = 0; index < args.length; index += 2) {
    const flag = args[index]
    const value = args[index + 1]
    if (flag === '--threshold' && threshold === undefined) {
      const parsed = Number(value)
      if (value === undefined || !/^(\d+\.?\d*|\.\d+)$/.test(value) || !(parsed >= 0 && parsed <= 1)) {
        return { kind: 'error', message: '--threshold には 0 以上 1 以下の数を指定してください（例: 0.05）。' }
      }
      threshold = parsed
    } else if (flag === '--direction' && direction === undefined) {
      if (value !== 'ge' && value !== 'le') return { kind: 'error', message: '--direction には ge か le を指定してください。' }
      direction = value
    } else {
      return { kind: 'error', message: 'answer --threshold X --direction ge|le か、answer --message <文字列> の形で指定してください。' }
    }
  }
  if (threshold === undefined || direction === undefined) {
    return { kind: 'error', message: 'answer には --threshold と --direction の両方が必要です。' }
  }
  return { kind: 'answer', submission: { threshold, direction } }
}

function parseSubmit(args: string[]): TerminalCommand {
  const command: { kind: 'submit'; shots?: number; initialization?: 'prepare' | 'inherit'; interleave?: InterleaveReference; measure?: Record<number, number> } = { kind: 'submit' }
  for (let index = 0; index < args.length; index += 1) {
    const flag = args[index]
    const value = args[index + 1]
    if (flag === '--shots') {
      const shots = Number(value)
      if (value === undefined || !/^\d+$/.test(value) || !Number.isSafeInteger(shots) || shots < 1) {
        return { kind: 'error', message: '--shots には正の整数を指定してください。' }
      }
      command.shots = shots
      index += 1
    } else if (flag === '--init') {
      if (value !== 'prepare' && value !== 'inherit') {
        return { kind: 'error', message: '--init には prepare か inherit を指定してください。' }
      }
      command.initialization = value
      index += 1
    } else if (flag === '--interleave') {
      const reference = value === 'other' ? 'victim' : value === 'ref:0' ? 'training:0' : value === 'ref:1' ? 'training:1' : undefined
      if (reference === undefined) {
        return { kind: 'error', message: '--interleave には other、ref:0、ref:1 のいずれかを指定してください。' }
      }
      command.interleave = reference
      index += 1
    } else if (flag === '--measure') {
      if (value === undefined) return { kind: 'error', message: '--measure には qN=cM を , 区切りで指定してください（例: q0=c1,q1=c0）。' }
      const measure = { ...(command.measure ?? {}) }
      for (const pair of value.split(',')) {
        const match = MEASURE_PAIR.exec(pair)
        if (!match) return { kind: 'error', message: `測定先の書き方が正しくありません: ${pair}（例: q0=c1）` }
        const qubit = Number(match[1])
        const bit = Number(match[2])
        if (qubit > MAX_QUBIT_INDEX || bit > MAX_QUBIT_INDEX) return { kind: 'error', message: `番号が大きすぎます: ${pair}` }
        if (qubit in measure) return { kind: 'error', message: `q${qubit} の測定先が2回指定されています。` }
        measure[qubit] = bit
      }
      command.measure = measure
      index += 1
    } else {
      return { kind: 'error', message: `submit の不明なオプションです: ${flag}` }
    }
  }
  if (command.interleave !== undefined) {
    if (command.initialization === 'prepare') {
      return { kind: 'error', message: '--interleave は前の状態を引き継ぐ観測です。--init inherit と併用してください（省略すると inherit になります）。' }
    }
    command.initialization = 'inherit'
  }
  return command
}
