export const FPS = 30;
export const SCENES = [
  {id: 'intro', from: 0, duration: 90, color: '#77baff'},
  {id: 'voice', from: 90, duration: 180, color: '#77baff'},
  {id: 'folders', from: 270, duration: 150, color: '#75e4c6'},
  {id: 'ai', from: 420, duration: 210, color: '#c1adff'},
  {id: 'export', from: 630, duration: 180, color: '#f1cf8c'},
  {id: 'outro', from: 810, duration: 90, color: '#77baff'},
] as const;
export const DURATION_IN_FRAMES = SCENES.reduce((sum, scene) => sum + scene.duration, 0);
