import { makeOptions, setupCheck, predict } from './common.js';

export const options = makeOptions('burst');
export const setup = setupCheck;
export default predict;
