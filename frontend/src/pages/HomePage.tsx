import './HomePage.css'
import { type Dispatch, type SetStateAction } from 'react'
import { HomeModeDrum } from '../components/HomeModeDrum'
import { QuantumPet } from '../components/QuantumPet'
import { useTutorial } from '../context/useTutorial'
import { homeTips } from '../utils/quantumPetTips'
import { tutorialCourseList } from '../utils/tutorialScript'

type HomePageProps = {
  /*
   * ドラムの回転数。App が持っている。背景のゆらぎは全画面で1枚きりなので、
   * 「今どの面が正面か」もそこまで届いている必要がある。
   */
  modeTurn: number
  onModeTurnChange: Dispatch<SetStateAction<number>>
  onStartSimulation: () => void
  onOpenPulseLab: () => void
  onOpenAttackSimulator: () => void
}

export function HomePage({
  modeTurn,
  onModeTurnChange,
  onStartSimulation,
  onOpenPulseLab,
  onOpenAttackSimulator,
}: HomePageProps) {
  const tutorial = useTutorial()

  return (
    /* 地は塗らない。App が敷いている真空のゆらぎを透かして見せる。 */
    <main className="home-page">
      <div className="tt-hazard" aria-hidden="true" />

      <section className="home-page__hero" data-tutorial-anchor="home-hero">
        <h1 className="home-page__wordmark">
          <span>YURAGI</span>
          <span>STRIDER</span>
        </h1>
      </section>

      {/*
        入口はここ1か所に集約した。以前は2つのモードを左右に並べていたが、
        「どちらを選ぶか」を横並びで迫るより、1つずつ正面に出して読ませる方が
        中身の違いが伝わる。公式ドキュメントも同格の行き先として同じ列に置く。
      */}
      <div className="home-page__selector">
        <HomeModeDrum
          turn={modeTurn}
          onTurnChange={onModeTurnChange}
          onStartSimulation={onStartSimulation}
          onOpenPulseLab={onOpenPulseLab}
          onOpenAttackSimulator={onOpenAttackSimulator}
        />
      </div>

      {/*
        はじめて来た人の入口。モード選択の下に置いて、
        「まず何をすればいいか分からない」で止まらないようにする。
        1本目で使い方、2本目で物理の実験、と役割を分けている。
      */}
      <section
        className="home-page__tutorial"
        aria-labelledby="home-page-tutorial-heading"
      >
        <div className="home-page__tutorial-intro">
          <span className="home-page__tutorial-unit">Guided tour / ナビペットが案内します</span>
          <h2
            id="home-page-tutorial-heading"
            className="home-page__tutorial-heading"
          >
            チュートリアル
          </h2>
          <p className="home-page__tutorial-lede">
            量子回路を知らなくても大丈夫。画面の見るべき場所を光らせながら、
            手を動かして進みます。まずはここから。
          </p>
        </div>

        <ul className="home-page__tutorial-list">
          {tutorialCourseList.map((course, index) => {
            const isCompleted = tutorial.completedCourses.includes(course.id)
            return (
              <li className="home-page__tutorial-course" key={course.id}>
                <div className="home-page__tutorial-course-head">
                  <data className="home-page__tutorial-index" value={index + 1}>
                    {String(index + 1).padStart(2, '0')}
                  </data>
                  <h3 className="home-page__tutorial-name">{course.title}</h3>
                  <span className="home-page__tutorial-meta">
                    {isCompleted ? `修了済み / ${course.duration}` : course.duration}
                  </span>
                </div>
                <p className="home-page__tutorial-summary">{course.summary}</p>
                <button
                  className="home-page__tutorial-button"
                  type="button"
                  onClick={() => tutorial.start(course.id)}
                >
                  {isCompleted ? 'もう一度受ける' : 'はじめる'}
                </button>
              </li>
            )
          })}
        </ul>
      </section>

      <footer className="home-page__colophon">
        <span>&copy; YURAGI&ndash;STRIDER</span>
        <span>PYTHON / RUST PREVIEW BACKEND</span>
        <span>NO WARRANTY &mdash; RESEARCH INSTRUMENT</span>
      </footer>

      {/* ホームでもナビペットは常駐する。チュートリアルの入口はここにも置く。 */}
      <QuantumPet
        phase="idle"
        tips={homeTips}
        actions={tutorialCourseList.map((course, index) => (
          <button
            className={`quantum-pet__action${index === 0 ? '' : ' quantum-pet__action--quiet'}`}
            type="button"
            key={course.id}
            onClick={() => tutorial.start(course.id)}
          >
            {course.title}
          </button>
        ))}
      />
    </main>
  )
}
