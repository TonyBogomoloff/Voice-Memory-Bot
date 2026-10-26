import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {HELP_DEMOS, HelpTopic, HelpVideo} from './help-video';

const Root: React.FC = () => <>{(Object.keys(HELP_DEMOS) as HelpTopic[]).map(topic => <Composition
  key={topic} id={`Help-${topic}`} component={HelpVideo} defaultProps={{topic}}
  width={1080} height={1080} fps={30} durationInFrames={300}/>)}</>;
registerRoot(Root);
