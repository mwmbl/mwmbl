/**
 * A debounce function to reduce input spam
 * @param {*} callback Function that will be called
 * @param {*} timeout Minimum amount of time between calls
 * @returns The debounced function
 */
export default (callback, timeout = 100) => {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => { callback.apply(this, args); }, timeout);
  };
}
