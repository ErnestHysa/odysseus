// static/js/chatDeleteTargets.js
//
// Pure helper for the chat message delete flow: given the rendered message
// list and the index of the bubble the user clicked X on, return which
// sibling bubbles belong to the same user/AI exchange and which dbIds (if
// any) should be deleted server-side. The caller owns the actual DOM
// removal and the fetch.
//
// A message element only needs:
//   - classList.contains(name) -> bool
//   - dataset.dbId -> string | undefined
//
// Returns null if clickedIndex is out of range; otherwise
//   { userIndex, aiIndex, msgIds: string[] }.
// Both indices are -1 when no matching partner exists. msgIds is empty
// when the bubbles were never persisted (e.g. client-side "No chat
// session active" error bubbles shown when no model is selected).

export function computeDeleteTargets(allMsgs, clickedIndex) {
  if (clickedIndex < 0 || clickedIndex >= allMsgs.length) return null;
  const clicked = allMsgs[clickedIndex];
  const clickedIsUser = clicked.classList.contains('msg-user');

  let userIndex = -1;
  let aiIndex = -1;

  if (clickedIsUser) {
    userIndex = clickedIndex;
    for (let i = clickedIndex + 1; i < allMsgs.length; i++) {
      const m = allMsgs[i];
      if (m.classList.contains('msg-ai') && !m.classList.contains('msg-continuation')) {
        aiIndex = i;
        break;
      }
      if (m.classList.contains('msg-user')) break; // next user msg, no AI response
    }
  } else {
    let mainAiIndex = clickedIndex;
    if (allMsgs[mainAiIndex].classList.contains('msg-continuation')) {
      for (let i = mainAiIndex - 1; i >= 0; i--) {
        const m = allMsgs[i];
        if (m.classList.contains('msg-ai') && !m.classList.contains('msg-continuation')) {
          mainAiIndex = i;
          break;
        }
      }
    }
    aiIndex = mainAiIndex;
    for (let i = aiIndex - 1; i >= 0; i--) {
      if (allMsgs[i].classList.contains('msg-user')) {
        userIndex = i;
        break;
      }
    }
  }

  const msgIds = [];
  if (userIndex >= 0) {
    const uid = allMsgs[userIndex].dataset && allMsgs[userIndex].dataset.dbId;
    if (uid) msgIds.push(uid);
  }
  if (aiIndex >= 0) {
    const aid = allMsgs[aiIndex].dataset && allMsgs[aiIndex].dataset.dbId;
    if (aid) msgIds.push(aid);
  }

  return { userIndex, aiIndex, msgIds };
}
