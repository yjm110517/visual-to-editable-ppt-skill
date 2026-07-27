# Element Classification and Asset Safety

Use this reference when the Planner classifies a visual element or when a deterministic command accepts an asset. Optimize for editability only after meeting the visual-fidelity threshold. Do not interpret "native first" as permission to replace a distinctive source icon with a generic symbol.

## Representation decision

Inventory every non-text visual motif before writing `layout.json`. Record one `representation_decision` for each icon, illustration, photograph, texture, brand mark, or background decoration.

Choose the representation in this order:

1. Use native PowerPoint only when the visual can be reproduced faithfully with basic geometry, supported fills, lines, and text.
2. Use sanitized SVG for a flat vector visual with complex paths but without photographic detail, texture, 3D rendering, or source-specific soft effects.
3. Crop and re-encode the source as PNG or JPEG when visual identity depends on gradients, highlights, inner shadows, soft shadows, transparency, 3D treatment, texture, irregular detail, or photographic detail.

The following are not acceptable substitutes for a distinctive source icon:

- Unicode glyphs, emoji, dingbats, or letters;
- a generic circle, triangle, square, or hexagon;
- a newly drawn flat symbol that discards source gradients, depth, highlights, shadows, or characteristic contours.

If a source icon is already isolated inside a text-free tile, crop the icon or tile and keep surrounding card text, borders, and connectors native. Crop the smallest region that preserves the complete effect without including unrelated readable text.

Use this decision table:

| Source visual | Required representation |
|---|---|
| Text, cards, dividers, simple arrows, basic geometric diagrams | Native PowerPoint |
| Flat vector icon with source-independent styling | Native PowerPoint or sanitized SVG |
| Polished UI icon with gradients, highlights, soft shadow, 3D depth, or irregular detail | Cropped PNG |
| Photograph, portrait, textured illustration, or rendered object | Cropped PNG/JPEG |
| Logo or seal whose text cannot be separated | Cropped asset with a valid text exemption |
| Whole slide or text-bearing content card | Forbidden rasterization |

## Native PowerPoint objects

Use native PowerPoint text and shapes for:

- every required editable text item;
- cards, panels, borders, dividers, lines, arrows, badges, and genuinely simple icons whose source appearance is preserved by the supported primitives;
- flat fills, gradients that PowerPoint can reproduce, and basic geometric diagrams.

Do not rasterize a complete slide, a text-bearing card, or a group that can reasonably be rebuilt from native objects. Give every native object a stable element ID in `layout.json`.

## PNG and JPEG assets

Use a raster asset for a complex photographic, textured, illustrated, rendered, or source-specific icon region whose internal parts do not need editing.

- Use PNG for transparency or lossless graphic regions. Preserve source alpha when it exists.
- Use JPEG only for opaque RGB photographic regions.
- Crop only from an explicitly authorized source image and a validated `crops.json` entry.
- Require the original and padded crop bounds to remain fully inside the source. Never silently clamp a crop.
- Re-encode the pixels and remove EXIF, XMP, comments, and other nonessential metadata.
- Do not use automatic background removal in P2.

## SVG assets

Use SVG for a vector illustration or icon that cannot be represented faithfully with simple PowerPoint shapes. Every SVG must pass `sanitize_svg.py` before a builder can resolve it.

Reject an SVG containing scripts, event handlers, `foreignObject`, raster images, DOCTYPE or entities, external or file links, data URIs, remote CSS, or text elements. Allow only local fragment references such as `url(#gradient)` and `href="#symbol"`. Require a finite `viewBox` with positive width and height.

Never overwrite the original SVG. The sanitizer writes a deterministic `*.sanitized.svg`, records both hashes in `svg_security_report.json`, and makes the sanitized path the build-ready manifest path.

## Text and editability exemptions

Readable slide text must be native PowerPoint text. If inseparable text is part of a logo, seal, artwork, or brand mark, declare all of the following on the asset:

```json
{
  "contains_text": true,
  "text_editability_exempt": true,
  "exemption_reason": "brand_logo"
}
```

An asset with `contains_text: true` and no valid exemption is a hard validation failure. SVG `text`, `tspan`, and `textPath` are always rejected; convert decorative SVG text to paths before sanitization and still declare the applicable exemption.

## Build boundary

The builder receives asset IDs, never arbitrary filesystem paths. `validate_assets.py` is the authority for mapping an ID to a build-ready file. It verifies the manifest Schema, allowlist membership, canonical path containment, absence of links or reparse points, extension and real media type, dimensions, size, SHA-256, text exemption, and SVG security report.

The accepted asset chain is:

```text
source image / original SVG
→ validated crop or strict SVG sanitization
→ asset_manifest.json with final file hashes
→ validate_assets.py allowlist
→ deterministic asset ZIP or PPT builder
```
