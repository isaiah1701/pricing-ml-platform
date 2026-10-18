import { makeOptions, setupCheck, predict } from './common.js';

export const options = makeOptions('baseline');
export const setup = setupCheck;
export default predict;
