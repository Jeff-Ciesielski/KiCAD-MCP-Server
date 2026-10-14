/**
 * Schematic text note and junction tools: edit and delete text notes, and
 * list, add and delete junctions.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult } from "./tool-response.js";

const point = z.object({ x: z.number(), y: z.number() });

const JUNCTION_RULE =
  "A junction joins the wires (or buses) at its point only where 3 or more connections meet: " +
  "each wire end and pin counts one, and a wire passing through counts two. So a T, a " +
  "crossing, or a pin on the middle of a wire can take a junction; a corner or a wire end on " +
  "a pin cannot. KiCad joins a wire end to the middle of another wire only through a junction.";

export function registerSchematicMarkupTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "edit_schematic_text",
    "Change one text note (a (text) or (text_box) item) in place: its text, position, angle, " +
      "font size, bold, italic or justification. Fields you leave out are kept, and the note " +
      "keeps its uuid. Pick the note by uuid (from list_schematic_texts) or by its current " +
      "position (nearest note within 0.5 mm). Returns the note before and after.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      uuid: z.string().optional().describe("uuid of the note, from list_schematic_texts"),
      position: point.optional().describe("Current position of the note, if no uuid is given"),
      text: z
        .string()
        .optional()
        .describe("New text; line breaks are kept. Use delete_schematic_text to remove a note."),
      newPosition: point.optional().describe("Move the note here, in mm"),
      angle: z.number().optional().describe("Rotation in degrees"),
      fontSize: z.number().optional().describe("Font size in mm"),
      bold: z.boolean().optional(),
      italic: z.boolean().optional(),
      justify: z
        .enum(["left", "center", "right"])
        .optional()
        .describe("Horizontal justification; the vertical one is kept"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("edit_schematic_text", args)),
  );

  server.tool(
    "delete_schematic_text",
    "Delete one text note (a (text) or (text_box) item). Pick it by uuid (from " +
      "list_schematic_texts) or by its position (nearest note within 0.5 mm). Returns the " +
      "deleted note.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      uuid: z.string().optional().describe("uuid of the note, from list_schematic_texts"),
      position: point.optional().describe("Position of the note, if no uuid is given"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("delete_schematic_text", args)),
  );

  server.tool(
    "list_schematic_junctions",
    "List the junctions on one sheet. Each has its position, uuid, the wire and bus " +
      "connections at its point, and joinsAnything: false marks a junction with fewer than 3 " +
      "connections, which joins nothing. " +
      JUNCTION_RULE,
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("list_schematic_junctions", args)),
  );

  server.tool(
    "add_schematic_junction",
    "Add a junction at a point. " +
      JUNCTION_RULE +
      " A point with fewer than 3 connections is refused. The wire tools already add a junction " +
      "where a new wire ends on another wire; use this one for a crossing that must join, or to " +
      "put back a junction that was lost.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      position: point.describe("Junction position in mm, exactly on the wires"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("add_schematic_junction", args)),
  );

  server.tool(
    "delete_schematic_junction",
    "Delete the junction nearest a point, within 0.5 mm. The wires at a crossing are then no " +
      "longer joined. Where a wire ends at the point and 3 or more connections still meet, the " +
      "result has a warning: KiCad and the wire tools put such a junction back on the next " +
      "wire edit, so move or delete the wire to break that connection.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch file"),
      position: point.describe("Junction position in mm"),
    },
    async (args: any) =>
      formatKicadResult(await callKicadScript("delete_schematic_junction", args)),
  );
}
