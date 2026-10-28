/* Shared presentation contract for saved observations. No action inference occurs here. */
(function (root) {
  'use strict';
  const labels = Object.freeze({
    pick_up: 'Pick up item', pickup: 'Pick up item', picked_up_item: 'Pick up item',
    touch: 'Touch item', touch_product: 'Touch item', put_down: 'Put down item',
    put_back: 'Put back item', put_back_item: 'Put back item', rotate_item: 'Turn item',
    examine_object: 'Examine item', inspect_product: 'Inspect product',
    hand_over: 'Hand over item', place_in_basket: 'Place in basket',
    carry: 'Carry item', drop_item: 'Drop item', conceal_item: 'Conceal item',
    remove_packaging: 'Remove packaging', exit_with_item: 'Leave with item',
    place_elsewhere: 'Place item', reach_toward: 'Reach toward item',
    enter_view: 'Enter view', leave_view: 'Leave view', exit_view: 'Leave view',
    entered_queue: 'Join queue', reached_checkout: 'Reach checkout', reach_checkout: 'Reach checkout',
    left_queue: 'Leave queue', queue_exit: 'Leave queue', left_checkout: 'Leave checkout',
    handed_cash: 'Hand over cash', cash_handover: 'Hand over cash',
    presented_card: 'Present card', present_card: 'Present card',
    sit_down: 'Sit down', stand_up: 'Stand up', bend_down: 'Bend down',
    walk: 'Walk', walking: 'Walk', walk_through: 'Walk through',
    stand: 'Stand', standing: 'Stand', stationary: 'Stay in place',
    display_empty: 'Empty display', stock_out: 'Empty display', out_of_stock: 'Empty display',
    display_item_added: 'Item added to display', display_item_removed: 'Item removed from display',
    product_misplaced: 'Misplaced product', table_dirty: 'Dirty table',
    dirty_table_occupied: 'Occupied dirty table', table_cleaned: 'Table cleaned',
    restocked: 'Display restocked', fall: 'Fall', observe: 'Observe'
  });
  let registry = Object.create(null);
  function configure(profile) { registry = Object.assign(Object.create(null), profile?.event_types || {}); }
  // Rendering rules for a profile that names its events and leaves emphasis and timers to the page: the actions
  // that change what a visitor holds or pays are major, and looking and examining are timed while they last.
  // A profile that states an emphasis for an event decides that event.
  const majorActions = new Set(['pick_up', 'pickup', 'picked_up_item', 'put_down', 'put_back', 'put_back_item',
    'place_in_basket', 'hand_over', 'drop_item', 'conceal_item', 'handed_cash', 'cash_handover', 'presented_card', 'present_card']);
  const emphasis = type => registry[type]?.emphasis || (majorActions.has(type) ? 'major' : 'normal');
  const generic = new Set(['other_observation','other_observed_action','miscellaneous','other','unknown']);
  function clean(value) {return String(value ?? '').replace(/\s+/g, ' ').trim();}
  function canonicalActionTitle(event) {
    const action = event?.event || event?.type || '';
    if (registry[action]?.label) return registry[action].label;
    if (labels[action]) return labels[action];
    if (generic.has(action)) {
      const explicit = event?.raw_type || event?.event_label;
      return labels[explicit] || 'Observation';
    }
    const text = clean(action).replaceAll('_', ' ');
    return text ? text[0].toUpperCase() + text.slice(1) : 'Observation';
  }
  function eventDescription(event) {
    // Evidence describes the action; object description supplements it elsewhere.
    let text = clean(event?.evidence || event?.item_description || event?.person_description || event?.description);
    // Some saved outputs repeat the exact same sentence block twice.
    if (text.length % 2 === 1) {
      const mid = Math.floor(text.length / 2);
      if (text[mid] === ' ' && text.slice(0, mid) === text.slice(mid + 1)) text = text.slice(0, mid);
    }
    text = text.replace(/^Person\s+(?:P?\d+)\s+/i, '');
    return text ? text[0].toUpperCase() + text.slice(1) : '';
  }
  function personKey(recordingId, personId) {
    return `${encodeURIComponent(String(recordingId))}/person/${encodeURIComponent(String(personId))}`;
  }
  function stableEventId(recordingId, windowIndex, eventIndex, event) {
    // Source event IDs are already recording-scoped; fallback addresses the preserved payload.
    return `${encodeURIComponent(String(recordingId))}/event/${encodeURIComponent(event?.event_id ?? `${windowIndex}:${eventIndex}`)}`;
  }
  function eventAnchorFromTracks(tr, personId, time_s) {
    if (!tr?.frames || !Number.isFinite(time_s) || time_s < 0 || !(tr.fps > 0)) return null;
    const frames = tr.frameNumbers || Object.keys(tr.frames).map(Number).sort((a, b) => a - b);
    const stamp = frame => Number.isFinite(tr.frame_times_s?.[frame]) ? tr.frame_times_s[frame] : frame / tr.fps;
    let lo = 0, hi = frames.length - 1, previous = -1;
    while (lo <= hi) {const mid = (lo + hi) >> 1; if (stamp(frames[mid]) <= time_s + 1e-6) {previous = mid; lo = mid + 1;} else hi = mid - 1;}
    if (previous < 0) return null;
    const frame = frames[previous], at = stamp(frame), nominal = Math.max(1, tr.sampleStep || 1) / tr.fps;
    if (time_s - at >= nominal + 1e-5) return null;
    const item = tr.frames[String(frame)]?.find(item => String(item.id) === String(personId));
    const box = item?.box;
    if (!box || box.length !== 4 || !box.every(Number.isFinite)) return null;
    // Footpoint of the observed 2D person box, not an object contact point or world coordinate.
    return {frame, x: (box[0] + box[2]) / 2, y: box[3], observed_at_s: at, basis: 'person_box_footpoint_2d'};
  }
  function activityAt(events, personId, time_s) {
    const actor = value => String(value ?? '').trim().replace(/^Person\s+/i, '');
    const valid = (events || []).filter(e => e.eligible_for_overlay !== false &&
      (e.person_ids || [e.person_id]).some(id => id != null && actor(id) === actor(personId)) &&
      Number.isFinite(e.playback_start_s) && Number.isFinite(e.playback_end_s) && e.playback_end_s >= e.playback_start_s
    ).sort((a, b) => a.playback_start_s - b.playback_start_s);
    let joined = null;
    for (const e of valid) {
      if (e.event === 'entered_queue' && joined === null) joined = e;
      if (joined && ['reached_checkout', 'left_queue', 'queue_exit'].includes(e.event)) {
        if (time_s >= joined.playback_start_s && time_s < e.playback_start_s)
          return {label: 'Waiting in queue', start_s: joined.playback_start_s, end_s: e.playback_start_s,
            elapsed_s: time_s - joined.playback_start_s, event_id: joined.event_id || joined.key};
        joined = null;
      }
    }
    const sustained = Object.fromEntries(Object.entries(registry).filter(([,spec]) => spec.timer_label).map(([type,spec]) => [type,spec.timer_label]));
    if (!Object.keys(sustained).length) Object.assign(sustained, {inspect_product: 'Inspecting product', examine_object: 'Examining item', look_at_display: 'Looking at display'});
    const e = valid.find(e => sustained[e.event] && time_s >= e.playback_start_s && time_s < e.playback_end_s);
    return e ? {label: sustained[e.event], start_s: e.playback_start_s, end_s: e.playback_end_s,
      elapsed_s: time_s - e.playback_start_s, event_id: e.event_id || e.key} : null;
  }
  function isProductInteraction(event) {return registry[event?.event]?.evidence_kind === 'interaction';}
  function majorInteractionAt(events, personId, time_s) {
    const actor = id => String(id ?? '').trim().replace(/^Person\s+/i, '');
    const e = (events || []).filter(e => emphasis(e.event) === 'major' && e.eligible_for_overlay !== false &&
      (e.person_ids || [e.person_id]).some(id => id != null && actor(id) === actor(personId)) &&
      Number.isFinite(e.playback_start_s) && time_s >= e.playback_start_s && time_s < e.playback_start_s + .45
    ).sort((a, b) => b.playback_start_s - a.playback_start_s)[0];
    if (!e) return null;
    const phase = (time_s - e.playback_start_s) / .45;
    return {event_id: e.event_id || e.key, opacity: Math.sin(Math.PI * phase)};
  }
  const api = Object.freeze({configure, canonicalActionTitle, eventDescription, stableEventId, personKey, eventAnchorFromTracks, activityAt, isProductInteraction, majorInteractionAt});
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.JourneyModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
