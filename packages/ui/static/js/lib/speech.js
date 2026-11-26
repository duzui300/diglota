/**
 * Speech synthesis and sentence-by-sentence read-along playback.
 */

import { $, $$, toast } from './dom.js';
import { pickVoiceFor, targetName, ttsLangFor, ttsRateFor } from './language.js';

let targetVoice = null;
let baseVoice = null;

export function pickVoice() {
  const voices = (typeof window !== 'undefined' && window.speechSynthesis)
    ? speechSynthesis.getVoices()
    : [];
  targetVoice = pickVoiceFor(voices, 'target');
  baseVoice = pickVoiceFor(voices, 'base');
}

export function initSpeech() {
  if (typeof window !== 'undefined' && window.speechSynthesis) {
    pickVoice();
    speechSynthesis.addEventListener('voiceschanged', pickVoice);
  }
}

export const reader = { active: false, token: 0, node: null };

function utteranceFor(text, role) {
  const utterance = new SpeechSynthesisUtterance(text);
  const isTarget = role === 'es' || role === 'target';
  const voice = isTarget ? targetVoice : baseVoice;
  const roleKey = isTarget ? 'target' : 'base';
  utterance.lang = ttsLangFor(roleKey, voice);
  if (voice) utterance.voice = voice;
  utterance.rate = ttsRateFor(roleKey);
  return utterance;
}

function speakAsync(text, role, token) {
  return new Promise((resolve) => {
    if (typeof window === 'undefined' || !window.speechSynthesis || reader.token !== token || !text.trim()) {
      resolve();
      return;
    }
    const utterance = utteranceFor(text, role);
    const done = () => resolve();
    utterance.onend = done;
    utterance.onerror = done;
    speechSynthesis.speak(utterance);
  });
}

/* The runs of one sentence, with adjacent runs of the same language joined.
 * This is what puts a target-language sentence back together after a bold span split
 * it: three fragments become one utterance, as they were written. */
function sentenceRuns(sentence) {
  const runs = [];
  for (const child of sentence.children) {
    if (child.classList.contains('gloss')) continue;
    const role = child.classList.contains('es') ? 'target' : 'base';
    const text = child.textContent.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const last = runs[runs.length - 1];
    if (last && last.role === role) last.text += ` ${text}`;
    else runs.push({ role, text });
  }
  return runs;
}

function highlight(node) {
  if (reader.node) reader.node.classList.remove('speaking');
  reader.node = node;
  if (node) node.classList.add('speaking');
}

function setReadButton(active) {
  const button = $('#btn-read-aloud');
  if (!button) return;
  button.textContent = active ? '⏸ Stop reading' : '▶ Read aloud';
  button.classList.toggle('active', active);
}

export function stopReading() {
  reader.active = false;
  reader.token += 1;
  if (typeof window !== 'undefined' && window.speechSynthesis) speechSynthesis.cancel();
  highlight(null);
  setReadButton(false);
}

export async function readAloud(fromNode) {
  const sentences = $$('#prose .sent');
  if (!sentences.length) { toast('Nothing to read here'); return; }
  stopReading();

  reader.active = true;
  reader.token += 1;
  const myToken = reader.token;
  const start = fromNode ? Math.max(sentences.indexOf(fromNode), 0) : 0;
  setReadButton(true);

  for (let i = start; i < sentences.length; i += 1) {
    if (!reader.active || reader.token !== myToken) return;
    const sentence = sentences[i];
    highlight(sentence);
    sentence.scrollIntoView({ block: 'center', behavior: 'smooth' });
    for (const run of sentenceRuns(sentence)) {
      if (reader.token !== myToken) return;
      await speakAsync(run.text, run.role, myToken);
    }
    if (reader.token !== myToken) return;
    await new Promise((resolve) => setTimeout(resolve, 120));
  }
  if (reader.token === myToken) stopReading();
}

export function toggleReading() {
  if (reader.active) stopReading(); else readAloud(reader.node || null);
}

export function speak(text) {
  if (typeof window === 'undefined' || !window.speechSynthesis) {
    toast('This browser has no speech synthesis', 'err');
    return;
  }
  stopReading();
  speechSynthesis.speak(utteranceFor(text, 'target'));
  if (!targetVoice) toast(`No ${targetName()} voice is installed. Using the default.`);
}
