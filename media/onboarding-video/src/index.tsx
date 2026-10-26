import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {VoiceMemoryIntro} from './video';
import {VoiceMemorySquare} from './square';
import {DURATION_IN_FRAMES, FPS} from './timing';

const Root: React.FC = () => <><Composition id="VoiceMemoryIntro" component={VoiceMemoryIntro}
  width={720} height={1280} fps={FPS} durationInFrames={DURATION_IN_FRAMES}/>
  <Composition id="VoiceMemorySquare" component={VoiceMemorySquare} width={1080} height={1080}
    fps={FPS} durationInFrames={DURATION_IN_FRAMES}/></>;
registerRoot(Root);
