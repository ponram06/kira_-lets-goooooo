/**
 * avatar/src/main/menu.js
 * Pure builders for the tray menu and the avatar menu (hover button or
 * right-click). They return plain Electron menu templates, so they can be
 * unit-tested without Electron.
 *
 * state:   { visible, micMuted, persona, personas, mode, freeAvailable,
 *            sleeping, size, locked, feedVisible, connected }
 * actions: callbacks — toggleVisible, setMicMuted, setPersona, openSettings,
 *          stopTask, quit, setSleeping, setRestricted, requestFree, setSize,
 *          setLocked, setFeedVisible
 */

const { SIZE_NAMES } = require('../shared/sizes');

function cap(s) {
  return String(s || '').charAt(0).toUpperCase() + String(s || '').slice(1);
}

function personaSubmenu(state, actions) {
  const personas = Array.isArray(state.personas) && state.personas.length ? state.personas : ['kira', 'ryuk'];
  return personas.map((p) => ({
    label: cap(p),
    type: 'radio',
    checked: state.persona === p,
    enabled: Boolean(state.connected),
    click: () => actions.setPersona(p)
  }));
}

function micItem(state, actions) {
  return {
    label: state.micMuted ? 'Unmute microphone' : 'Mute microphone',
    enabled: Boolean(state.connected),
    click: () => actions.setMicMuted(!state.micMuted)
  };
}

/** Tray icon menu. */
function buildTrayTemplate(state, actions) {
  return [
    { label: state.visible ? 'Hide avatar' : 'Show avatar', click: () => actions.toggleVisible() },
    micItem(state, actions),
    { label: 'Persona', submenu: personaSubmenu(state, actions) },
    { label: 'Settings…', click: () => actions.openSettings() },
    { label: 'Stop current task', enabled: Boolean(state.connected), click: () => actions.stopTask() },
    { type: 'separator' },
    { label: 'Quit Kira', click: () => actions.quit() }
  ];
}

/** Avatar menu (hover control button or right-click on the character). */
function buildAvatarTemplate(state, actions) {
  const free = state.mode === 'free';
  return [
    micItem(state, actions),
    {
      label: state.sleeping ? 'Wake' : 'Sleep',
      enabled: Boolean(state.connected),
      click: () => actions.setSleeping(!state.sleeping)
    },
    { label: 'Persona', submenu: personaSubmenu(state, actions) },
    {
      label: 'Mode',
      submenu: [
        {
          label: 'Restricted',
          type: 'radio',
          checked: !free,
          enabled: Boolean(state.connected),
          click: () => actions.setRestricted()
        },
        {
          label: state.freeAvailable ? 'Free… (asks for PIN)' : 'Free (set a PIN in Settings first)',
          type: 'radio',
          checked: free,
          enabled: Boolean(state.connected && state.freeAvailable && !free),
          click: () => actions.requestFree()
        }
      ]
    },
    {
      label: 'Size',
      submenu: SIZE_NAMES.map((s) => ({
        label: cap(s),
        type: 'radio',
        checked: state.size === s,
        click: () => actions.setSize(s)
      }))
    },
    {
      label: 'Lock position',
      type: 'checkbox',
      checked: Boolean(state.locked),
      click: () => actions.setLocked(!state.locked)
    },
    {
      label: 'Show action feed',
      type: 'checkbox',
      checked: Boolean(state.feedVisible),
      click: () => actions.setFeedVisible(!state.feedVisible)
    },
    { type: 'separator' },
    { label: 'Settings…', click: () => actions.openSettings() },
    { label: 'Stop task', enabled: Boolean(state.connected), click: () => actions.stopTask() },
    { label: 'Quit Kira', click: () => actions.quit() }
  ];
}

module.exports = { buildTrayTemplate, buildAvatarTemplate };
