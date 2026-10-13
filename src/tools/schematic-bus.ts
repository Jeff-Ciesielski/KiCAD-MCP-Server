/**
 * Schematic bus tools: bus segments, bus entries, bus aliases, the bus report,
 * and connecting a run of pins to a bus in one call.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult } from "./tool-response.js";

const point = z.object({ x: z.number(), y: z.number() });

const BUS_LABEL_SYNTAX =
  "A bus gets its members from a bus label placed on it with add_schematic_net_label, or, " +
  "with no label, from the bus sheet pins it reaches. Label syntax: " +
  "a vector such as 'D[0..7]' (members D0..D7) or a group such as 'I2C{SDA SCL}' (members " +
  "I2C.SDA, I2C.SCL; without a prefix the members are SDA, SCL). A group member can be a " +
  "vector or a bus alias: '{MEM}' or 'U{MEM}'.";

export function registerSchematicBusTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "add_schematic_bus",
    "Draw a bus through two or more waypoints. Each pair of waypoints becomes one straight " +
      "bus segment. A bus segment that the new bus ends on the middle of is split there, and " +
      "junctions are added where three or more bus ends meet or a bus ends on another bus. " +
      "Wires never connect to a bus directly: use add_schematic_bus_entry plus a labelled wire, " +
      "or connect_pins_to_bus. " +
      BUS_LABEL_SYNTAX,
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      waypoints: z
        .array(z.array(z.number()).length(2))
        .min(2)
        .describe("Ordered list of [x, y] coordinates in mm. Minimum 2 points."),
    },
    async (args: any) => formatKicadResult(await callKicadScript("add_schematic_bus", args)),
  );

  server.tool(
    "delete_schematic_bus",
    "Delete one bus segment by its start and end coordinates (either order). Junctions are resynced.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      start: point.describe("One end of the bus segment"),
      end: point.describe("The other end of the bus segment"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("delete_schematic_bus", args)),
  );

  server.tool(
    "add_schematic_bus_entry",
    "Add a 45 degree bus entry. Its bus end must sit on an existing bus; its wire end is " +
      "2.54 mm away in the given direction and is returned as wireEnd. Draw a wire from wireEnd " +
      "and put a net label naming the bus member on that wire: KiCad joins the wire to the " +
      "member its label names, and an unlabelled wire joins nothing.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      position: point.describe("Point on the bus where the entry starts, in mm"),
      direction: z
        .enum(["up_right", "down_right", "up_left", "down_left"])
        .optional()
        .describe("Direction from the bus to the wire end (default down_right; y grows down)"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("add_schematic_bus_entry", args)),
  );

  server.tool(
    "delete_schematic_bus_entry",
    "Delete the bus entry with an end within 0.5 mm of a position.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      position: point.describe("Either end of the entry, in mm"),
    },
    async (args: any) =>
      formatKicadResult(await callKicadScript("delete_schematic_bus_entry", args)),
  );

  server.tool(
    "list_schematic_buses",
    "Report the buses on one sheet. Each bus network lists its segments, its bus labels and " +
      "their expanded members, its bus entries with the net on the wire each one feeds and " +
      "whether that net is a member, and the sheet pins it reaches. 'problems' lists what KiCad " +
      "ERC would flag: entries feeding unlabelled wires or non-member nets, entries off any bus, " +
      "buses with neither a bus label nor a bus sheet pin, bus labels off any bus, and plain net " +
      "labels on a bus.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("list_schematic_buses", args)),
  );

  server.tool(
    "connect_pins_to_bus",
    "Connect symbol pins to an existing bus in one call. For each pin it adds a bus entry, a " +
      "straight wire from the entry to the pin, and a net label naming the member the pin " +
      "carries. A vertical bus feeds pins with horizontal wires at the pin's y; a horizontal " +
      "bus feeds them with vertical wires at the pin's x, so each pin must sit at least 2.54 mm " +
      "to one side of a bus segment. When the bus has a bus label, every net must be one of its " +
      "members. A bus with no label takes its members from its bus sheet pins. Nothing is " +
      "written if any pin cannot be connected.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      busPoint: point.describe("Any point on the target bus, in mm"),
      connections: z
        .array(
          z.object({
            reference: z.string().describe("Symbol reference, e.g. U1"),
            pin: z.union([z.string(), z.number()]).describe("Pin number or name"),
            net: z.string().describe("Bus member this pin carries, e.g. D3 or I2C.SDA"),
          }),
        )
        .min(1)
        .describe("One entry per pin"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("connect_pins_to_bus", args)),
  );

  server.tool(
    "list_bus_aliases",
    "List the bus aliases stored in the project file (KiCad 9+ keeps them in the .kicad_pro). " +
      "Pass the project file, or any schematic of the project to find it.",
    {
      schematicPath: z.string().optional().describe("A .kicad_sch of the project"),
      projectPath: z.string().optional().describe("The .kicad_pro file"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("list_bus_aliases", args)),
  );

  server.tool(
    "set_bus_alias",
    "Create or replace a bus alias in the project file. Members are net names or vectors " +
      "(e.g. ['D[0..7]', 'CLK', 'WE']). Use the alias inside braces in a bus label: '{NAME}' " +
      "for bare member names, 'PREFIX{NAME}' for PREFIX.<member>. A bare alias name is not a " +
      "bus label. KiCad reads aliases when it opens the project.",
    {
      schematicPath: z.string().optional().describe("A .kicad_sch of the project"),
      projectPath: z.string().optional().describe("The .kicad_pro file"),
      name: z.string().describe("Alias name, one word"),
      members: z.array(z.string()).min(1).describe("Member names or vectors"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("set_bus_alias", args)),
  );

  server.tool(
    "delete_bus_alias",
    "Delete a bus alias from the project file.",
    {
      schematicPath: z.string().optional().describe("A .kicad_sch of the project"),
      projectPath: z.string().optional().describe("The .kicad_pro file"),
      name: z.string().describe("Alias name"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("delete_bus_alias", args)),
  );
}
