import link from './link.js';
import verifyCode from './verify-code.js';
import addEmail from './add-email.js';
import verifyEmail from './verify-email.js';
import whoami from './whoami.js';
import seed from './seed.js';
import team from './team.js';
import myTeams from './my-teams.js';
import doc from './doc.js';
import help from './help.js';
import record from './record.js';
import bug from './bug.js';

// Single source of truth for the command set: consumed by the router (dispatch)
// and by registerCommands.js (Discord registration). Add a command = one import
// + one entry here.
export const commands = new Map([
  [link.name, link],
  [verifyCode.name, verifyCode],
  [addEmail.name, addEmail],
  [verifyEmail.name, verifyEmail],
  [whoami.name, whoami],
  [seed.name, seed],
  [team.name, team],
  [myTeams.name, myTeams],
  [doc.name, doc],
  [help.name, help],
  [record.name, record],
  [bug.name, bug],
]);

// Split a command list into release channels for registration:
// - stable commands register globally (every production server)
// - beta commands (`beta: true`) register ONLY to the dedicated
//   testing guild, so they stay exclusive to that server and never reach prod.
export function partitionCommands(list) {
  return {
    stable: list.filter((c) => !c.beta),
    beta: list.filter((c) => c.beta),
  };
}
