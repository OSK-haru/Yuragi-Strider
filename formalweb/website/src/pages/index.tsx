import {useEffect, useState, type ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';
import QuantumFluctuationField from '@site/src/components/QuantumFluctuationField';

/*
 * 本体アプリのホームの複製ではなく、製品紹介として読ませる面。
 * 本体ホームが「どのモードに入るか」を選ばせる操作盤なのに対し、
 * こちらは名前を初めて見た人に、何のための道具かを短く伝えるだけ。
 *
 * 数値も検証結果も機能一覧も置かない。それはドキュメント本体の仕事で、
 * ここに並べると入口が読みものになってしまう。この面に置くのは、
 * 何のための道具かを言う一文と、そこへの入口だけ。
 *
 * 下地には本体と同じ「真空のゆらぎ」を走らせて、要素はその上に浮かせる。
 */

type Slot = 'morning' | 'day' | 'evening' | 'night';

/* 時間帯ごとのあいさつと小ネタ。小ネタは訪問のたびに1つ選ぶ。 */
const GREETINGS: Record<Slot, {hello: string; jokes: string[]}> = {
  morning: {
    hello: 'おはようございます。',
    jokes: [
      'まどろみって、重ね合わせ？',
      '量子ビットたちの運命は酷なもので、二度目の朝を迎えることは少ない。',
      '"憂鬱ベクトル"に重ねられすぎないように。',
    ],
  },
  day: {
    hello: 'こんにちは。',
    jokes: [
      '自分自身の身長だの体重測定の結果を不確定性原理でごまかしてはならない。',
      '会議が長引くのはデコヒーレンスに似ている',
      '喝をいれられて、収縮する必要はないのです。',
    ],
  },
  evening: {
    hello: 'こんばんは。',
    jokes: [
      'あなたの残りT1が心配だ。',
      'うわ。漏れた！(高準位)',
      '夕飯を重ね合わせするのは、お勧めしない',
    ],
  },
  night: {
    hello: 'こんな時間ですね。',
    jokes: [
      '情熱的ですね..(⋈◍＞◡＜◍)。✧♡',
      'ありがとう。',
      'あなたは同志かも',
      'え？なに?、Qutipでよくないかって？"この不心得者が！"',
    ],
  },
};

function slotOf(hour: number): Slot {
  if (hour >= 5 && hour < 10) return 'morning';
  if (hour >= 10 && hour < 17) return 'day';
  if (hour >= 17 && hour < 22) return 'evening';
  return 'night';
}

/*
 * 時間は訪れた人のブラウザの時計で決める。ビルド時に決めると全員に同じ
 * あいさつが固定されるうえ、ハイドレーションも食い違うので、マウント後に選ぶ。
 * それまでは同じ高さの空枠を置き、差し替わってもレイアウトが動かないようにする。
 */
function Greeting(): ReactNode {
  const [text, setText] = useState<{hello: string; joke: string} | null>(null);

  useEffect(() => {
    const {hello, jokes} = GREETINGS[slotOf(new Date().getHours())];
    setText({hello, joke: jokes[Math.floor(Math.random() * jokes.length)]});
  }, []);

  return (
    <div className={styles.greeting} aria-live="polite">
      {text && (
        <>
          <p className={styles.hello}>{text.hello}</p>
          <p className={styles.joke}>{text.joke}</p>
        </>
      )}
    </div>
  );
}

function Masthead(): ReactNode {
  const {siteConfig} = useDocusaurusContext();

  return (
    <header className={styles.masthead}>
      <span className="eyebrow">Quantum circuit simulator</span>

      <Heading as="h1" className={styles.headline}>
        {siteConfig.title}
      </Heading>
      <p className={styles.tagline}>量子計算機をもっと身近に</p>

      <Greeting />

      {/* 入口その1。ヘッダのナビと、ページ下部と合わせて三か所。 */}
      <div className={styles.actions}>
        <Link
          className={styles.ctaPrimary}
          href="https://yuragi-strider-app.23sam55781.workers.dev">
          アプリを試す
        </Link>
        <Link className={styles.ctaGhost} to="/docs/overview/introduction">
          ドキュメントを読む
        </Link>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();

  return (
    <Layout title={siteConfig.title} description={siteConfig.tagline}>
      {/*
        下地はこの面の中に閉じる。position のスタッキングを .home 側で
        作って、ゆらぎをその最下層に敷き、中身を上に浮かせる。
      */}
      <main className={styles.home}>
        <QuantumFluctuationField />

        <div className={styles.content}>
          <Masthead />
        </div>
      </main>
    </Layout>
  );
}
