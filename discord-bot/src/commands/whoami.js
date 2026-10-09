import { defineCommand } from '../defineCommand.js';
import { buildWhoamiEmbed } from '../messages.js';
import { DirectoryUnavailable } from '../clients/directoryClient.js';

export default defineCommand({
  name: 'whoami',
  description: 'Show your directory record and linked accounts',
  auth: 'linked', // the router guarantees principal is present
  beta: false, // stable → registered globally (all prod servers)
  options: [],
  async handler({ principal, ctx }) {
    const person = principal.person;

    // Partial-view degrade: the caller is already authenticated, so a failed
    // identifiers fetch shouldn't hide the info the router just resolved.
    let identifiers;
    try {
      identifiers = await ctx.directory.listIdentifiers(person.id);
    } catch (e) {
      if (e instanceof DirectoryUnavailable) {
        identifiers = null;
      } else {
        throw e;
      }
    }

    return buildWhoamiEmbed(person, identifiers);
  },
});
