// errors: only failed calls in an agent's log; full: its whole summary; done: show finished agents without trouble
export type PanelView = { agent?: string; call?: string; errors?: boolean; full?: boolean; done?: boolean }

declare module 'claude-code' {
  interface PluginState {
    'software-architector': { isOpen: boolean; isPlaced: boolean; view: PanelView }
  }
}
