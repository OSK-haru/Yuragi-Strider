import {themes as prismThemes} from 'prism-react-renderer';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const config: Config = {
  title: 'Yuragi-Strider',
  tagline: '量子サービスの攻撃を試し、物理エンジンの挙動を観察するシミュレーター',
  favicon: 'img/favicon.svg',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // 本番URL。og:url / og:image / canonical はここから絶対URLが組まれるので、
  // 実際の配信ドメインと一致していないと SNS のカードが読み込めない。
  url: 'https://yuragi-strider.23sam55781.workers.dev',
  baseUrl: '/',

  onBrokenLinks: 'throw',

  // 本体アプリ (frontend/index.html) と同じ書体を読む。Archivo Black = macro
  // 見出し、JetBrains Mono = 計器ラベル / 数値、Inter = 本文。
  headTags: [
    {
      tagName: 'meta',
      attributes: {
        name: 'google-site-verification',
        content: 'uxEUHXelqrVlWkGvmS-Tm7Z11pr_vGnV9mixTRAkhrQ',
      },
    },
    {
      tagName: 'link',
      attributes: {rel: 'preconnect', href: 'https://fonts.googleapis.com'},
    },
    {
      tagName: 'link',
      attributes: {
        rel: 'preconnect',
        href: 'https://fonts.gstatic.com',
        crossorigin: 'anonymous',
      },
    },
  ],

  stylesheets: [
    'https://fonts.googleapis.com/css2?family=Archivo+Black&family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;600;800;900&display=swap',
  ],

  // Even if you don't use internationalization, you can use this field to set
  // useful metadata like html lang. Content is authored in Japanese.
  i18n: {
    defaultLocale: 'ja',
    locales: ['ja'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          remarkPlugins: [remarkMath],
          rehypePlugins: [rehypeKatex],
        },
        blog: false,

        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    // SNS 等に貼られたときの og:image。既定の Docusaurus カードのままだと
    // 全ページが他製品の名前とマスコットで共有されるので、本体と同じ
    // substrate で作った 1200x630 のカードを使う。
    image: 'img/yuragi-strider-social-card.jpg',

    /* 本体アプリと同様に、OS 設定を初期値として明暗を手動で切り替えられる。 */
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: false,
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Yuragi-Strider',
      logo: {
        alt: 'Yuragi-Strider Logo',
        src: 'img/logo-light.svg',
        srcDark: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'tutorialSidebar',
          position: 'left',
          label: 'ドキュメント',
        },
        {
          to: '/docs/learn/',
          label: '理論を学ぶ',
          position: 'left',
        },
        {
          to: '/docs/physics-model/overview',
          label: '物理モデル',
          position: 'left',
        },
        {
          to: '/docs/physics-model/validations/',
          label: '妥当性検証',
          position: 'left',
        },
        {
          href: 'https://github.com/OSK-haru/Yuragi-Strider',
          label: 'GitHub',
          position: 'right',
        },
        {
          href: 'https://yuragi-strider-app.23sam55781.workers.dev',
          label: 'アプリを試してみる',
          position: 'right',
          className: 'navbar__item--app-cta',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [],
      // 本体ホームの colophon と同じ並び。
      copyright: `&copy; ${new Date().getFullYear()} YURAGI&ndash;STRIDER &nbsp;/&nbsp; NO WARRANTY &mdash; RESEARCH INSTRUMENT`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.vsDark,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
