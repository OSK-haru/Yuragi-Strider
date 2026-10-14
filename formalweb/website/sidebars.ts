import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  tutorialSidebar: [
    {
      type: 'category',
      label: '概要',
      items: ['overview/introduction', 'overview/ui-design'],
    },
    {
      type: 'category',
      label: 'クイックスタート',
      items: ['getting-started/download'],
    },
    {
      type: 'category',
      label: 'チュートリアル',
      items: [
        'tutorials/index',
        {
          type: 'category',
          label: 'Gate-aware',
          items: [
            'tutorials/gate-aware/index',
            'tutorials/gate-aware/circuit-studio',
            'tutorials/gate-aware/simulate',
            'tutorials/gate-aware/algorithm-library',
            'tutorials/gate-aware/state-explorer',
            'tutorials/gate-aware/help',
          ],
        },
        {
          type: 'category',
          label: 'Pulse-level',
          items: [
            'tutorials/pulse-level/index',
            'tutorials/pulse-level/pulse-lab',
            'tutorials/pulse-level/pulse-circuit-studio',
            'tutorials/pulse-level/pulse-state-explorer',
          ],
        },
      ],
    },
    {
      type: 'category',
      label: '性能',
      items: ['performance/rust-acceleration'],
    },
    {
      // 「物理モデル詳細」が実装の仕様書なのに対し、こちらは同じ物理を
      // 導出から追う教科書。処理レイヤーと章が1対1に対応している。
      type: 'category',
      label: '理論を学ぶ',
      items: [
        'learn/index',
        'learn/quantum-state',
        'learn/unitary-dynamics',
        'learn/open-systems',
        'learn/decoherence',
        'learn/effective-hamiltonian',
        'learn/pulse-control',
        {
          // 第6章が概観、こちらがその先を独立コースとして履修する部分。
          type: 'category',
          label: 'パルス編(発展)',
          items: [
            'learn/pulse/index',
            'learn/pulse/transmon',
            'learn/pulse/envelopes',
            'learn/pulse/leakage-drag',
            'learn/pulse/qutrit-dissipation',
            'learn/pulse/quasi-static-noise',
            'learn/pulse/two-transmon',
            'learn/pulse/numerics',
          ],
        },
        'learn/numerical-integration',
        'learn/quantum-channels',
        'learn/metrics',
      ],
    },
    {
      type: 'category',
      label: '物理モデル詳細',
      items: [
        'physics-model/overview',
        'physics-model/assumuptions',
        'physics-model/input_and_parameters',
        'physics-model/lindblad',
        'physics-model/dissipation-model',
        {
          type: 'category',
          label: '制御モデル',
          items: [
            'physics-model/control_models/gate-awaremodel',
            'physics-model/control_models/pulse-levelmodel',
          ],
        },
        {
          type: 'category',
          label: '時間発展',
          items: [
            'physics-model/propagation/RK4',
            'physics-model/propagation/CPTP',
            'physics-model/propagation/statevector',
          ],
        },
        'physics-model/outputs',
        {
          type: 'category',
          label: '妥当性検証',
          items: [
            'physics-model/validations/index',
            'physics-model/validations/input',
            'physics-model/validations/propagation',
            'physics-model/validations/control-models',
            'physics-model/validations/readout-error',
            'physics-model/validations/hardware-comparison',
          ],
        },
        'physics-model/references',
      ],
    },
    {
      type: 'category',
      label: '制限事項',
      items: ['limitations/current-limitations', 'limitations/roadmap', 'limitations/attack-simulator'],
    },
  ],
};

export default sidebars;
