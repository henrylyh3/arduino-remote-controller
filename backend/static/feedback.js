(() => {
  let audioContext = null;

  function playClick() {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    audioContext ||= new AudioContext();
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();
    const now = audioContext.currentTime;
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(620, now);
    gain.gain.setValueAtTime(0.025, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.025);
    oscillator.connect(gain);
    gain.connect(audioContext.destination);
    oscillator.start(now);
    oscillator.stop(now + 0.025);
  }

  function interactionFeedback() {
    let vibrated = false;
    try {
      vibrated = typeof navigator.vibrate === "function" && navigator.vibrate(12);
    } catch {
      vibrated = false;
    }
    if (!vibrated) playClick();
  }

  window.smartHomeFeedback = interactionFeedback;
  document.addEventListener("click", (event) => {
    const control = event.target.closest("button, a.tab, [role='switch']");
    if (control && !control.disabled) interactionFeedback();
  }, { capture: true });
})();
