import { makeOptions, setupCheck, predict } from './common.js';

export const options = makeOptions('smoke');
export const setup = setupCheck;
export default predict;
