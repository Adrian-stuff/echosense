/* Hiding private content is synchronous; preserving work can finish behind the lock. */
export class PrivacyGate {
  constructor(content, screen, label) { this.content=content; this.screen=screen; this.label=label; this.locked=true; }
  lock(reason='Your session ended. Sign in again to unlock your family’s space.') {
    this.locked=true;
    this.content.hidden=true;
    this.content.inert=true;
    this.screen.hidden=false;
    if (this.label) this.label.textContent=reason;
    this.screen.focus?.({preventScroll:true});
  }
  unlock() { this.locked=false; this.screen.hidden=true; this.content.inert=false; this.content.hidden=false; }
}
