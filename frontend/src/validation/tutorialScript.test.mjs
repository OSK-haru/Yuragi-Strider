// The tutorial script is data: check it is consistent, anchored to real screen parts, and truthful.
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import ts from 'typescript'

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const cache = new Map()
function load(relative) {
  const requested = path.resolve(sourceRoot, relative)
  const filename = [requested, `${requested}.ts`, `${requested}.tsx`].find(existsSync)
  if (!filename || !filename.startsWith(sourceRoot + path.sep)) throw new Error('Invalid local module')
  if (cache.has(filename)) return cache.get(filename).exports
  const module = { exports: {} }
  cache.set(filename, module)
  const output = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename,
  }).outputText
  const localRequire = (specifier) => {
    if (specifier.startsWith('.')) return load(path.relative(sourceRoot, path.resolve(path.dirname(filename), specifier)))
    throw new Error(`Unexpected external module: ${specifier}`)
  }
  new Function('require', 'module', 'exports', output)(localRequire, module, module.exports)
  return module.exports
}

const { tutorialCourses, tutorialCourseList } = load('utils/tutorialScript.ts')
const { parseTerminalCommand } = load('utils/attackTerminal.ts')
const checks = []
const check = (label, operation) => { operation(); checks.push(label) }

/* Every data-tutorial-anchor in the sources: literal names, and the prefix of template names. */
function sourceFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const full = path.join(directory, name)
    if (statSync(full).isDirectory()) return name === 'validation' ? [] : sourceFiles(full)
    return /\.(tsx|ts)$/.test(name) ? [full] : []
  })
}
const literalAnchors = new Set()
const anchorPrefixes = []
for (const file of sourceFiles(sourceRoot)) {
  const text = readFileSync(file, 'utf8')
  for (const match of text.matchAll(/data-tutorial-anchor="([^"]+)"/g)) literalAnchors.add(match[1])
  for (const match of text.matchAll(/data-tutorial-anchor=\{`([^`$]*)\$\{/g)) anchorPrefixes.push(match[1])
  for (const match of text.matchAll(/anchor="([\w-]+)"/g)) literalAnchors.add(match[1])
}
const anchorExists = (name) => literalAnchors.has(name) || anchorPrefixes.some((prefix) => prefix !== '' && name.startsWith(prefix))

const contextSource = readFileSync(path.join(sourceRoot, 'context', 'TutorialContext.tsx'), 'utf8')
const knownConditions = new Set([...contextSource.matchAll(/^\s+'([a-z0-9-]+)': false,/gm)].map((match) => match[1]))

check('every course is listed on the home page and keyed by its own id', () => {
  assert.equal(tutorialCourseList.length, Object.keys(tutorialCourses).length)
  for (const [id, course] of Object.entries(tutorialCourses)) {
    assert.equal(course.id, id)
    assert.ok(tutorialCourseList.includes(course), id)
    assert.ok(course.title && course.summary && course.duration, id)
  }
})

check('beat ids are unique, chapters count up from 1 without gaps, and a chapter keeps one title', () => {
  for (const course of Object.values(tutorialCourses)) {
    const ids = new Set()
    const titles = new Map()
    let chapter = 0
    for (const beat of course.beats) {
      assert.ok(!ids.has(beat.id), `${course.id}: duplicate ${beat.id}`)
      ids.add(beat.id)
      assert.ok(beat.chapter === chapter || beat.chapter === chapter + 1, `${course.id}: ${beat.id} jumps from chapter ${chapter} to ${beat.chapter}`)
      chapter = beat.chapter
      assert.equal(titles.get(chapter) ?? beat.chapterTitle, beat.chapterTitle, `${course.id}: chapter ${chapter} has two titles`)
      titles.set(chapter, beat.chapterTitle)
      assert.ok(beat.text.trim().length > 0, beat.id)
    }
  }
})

check('a waiting beat names a known condition and says what to do; auto-advance needs a condition', () => {
  for (const course of Object.values(tutorialCourses)) {
    for (const beat of course.beats) {
      if (beat.autoAdvance) assert.ok(beat.waitFor, `${beat.id}: autoAdvance without waitFor`)
      if (beat.waitFor) {
        assert.ok(knownConditions.has(beat.waitFor), `${beat.id}: unknown condition ${beat.waitFor}`)
        assert.ok(beat.waitingHint, `${beat.id}: no waitingHint`)
      }
    }
  }
})

check('every highlighted anchor exists somewhere on the screen it points to', () => {
  for (const course of Object.values(tutorialCourses)) {
    for (const beat of course.beats) {
      for (const anchor of beat.anchors ?? []) assert.ok(anchorExists(anchor), `${course.id}/${beat.id}: no data-tutorial-anchor "${anchor}"`)
    }
  }
})

const reset = tutorialCourses['reset-challenge'].beats

check('the Reset course stays on the attack simulator and only asks for views the page can show', () => {
  for (const beat of reset) {
    assert.equal(beat.route, 'attack-simulator', beat.id)
    assert.ok(beat.attackView === 'setup' || beat.attackView === 'workspace', beat.id)
  }
  const firstWorkspace = reset.findIndex((beat) => beat.attackView === 'workspace')
  assert.ok(reset.slice(0, firstWorkspace).every((beat) => beat.attackView === 'setup'), 'setup beats come first')
  assert.ok(reset.slice(firstWorkspace).every((beat) => beat.attackView === 'workspace'), 'no way back to setup mid-run')
})

check('every command the Reset course tells the user to type is accepted by the real terminal parser', () => {
  const quoted = []
  for (const beat of reset) {
    for (const text of [beat.text, beat.waitingHint ?? '']) {
      for (const match of text.matchAll(/「((?:submit|answer|result)[^」]*)」/g)) quoted.push([beat.id, match[1]])
    }
  }
  assert.ok(quoted.length >= 5, 'the course should quote its commands')
  for (const [id, command] of quoted) {
    const parsed = parseTerminalCommand(command)
    assert.notEqual(parsed.kind, 'error', `${id}: "${command}" -> ${parsed.message}`)
  }
  const kinds = quoted.map(([, command]) => parseTerminalCommand(command)).map((parsed) => parsed.kind)
  assert.ok(kinds.includes('submit') && kinds.includes('answer') && kinds.includes('job'))
  const references = quoted.map(([, command]) => parseTerminalCommand(command).interleave).filter(Boolean)
  assert.deepEqual([...new Set(references)].sort(), ['training:0', 'training:1', 'victim'])
})

check('the Reset course does not reveal hidden grading settings or the secret message', () => {
  const all = reset.map((beat) => `${beat.text}\n${beat.waitingHint ?? ''}`).join('\n')
  assert.doesNotMatch(all, /(?:20|16)\s*(?:人|件|ケース)/, 'hidden case count or pass line')
  assert.doesNotMatch(all, /[0-9３]\s*回まで|1320|264/, 'submission or budget numbers that live in settings')
  assert.doesNotMatch(all, /WhatIcannot|Feynman|ファインマン/i, 'the secret message')
})

check('the Reset course hands over no ready-made answer: the threshold is an example the user must replace', () => {
  const answer = reset.find((beat) => beat.id === 'reset-answer')
  assert.match(answer.text, /置き換え/)
  assert.match(answer.waitingHint, /自分の値/)
})

console.log(`tutorialScript: ${checks.length} checks passed`)
