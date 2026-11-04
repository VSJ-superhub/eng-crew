---
name: ui-quality
description: The visual and content quality bar for user-facing UI, injected into a builder's prompt when the project has a web frontend. Used by SingleAgentEngineer.
---
=== UI QUALITY BAR ===
This project has a web frontend. Any screen you create or change goes to real
users and must look like a finished product, not a prototype. Working is not
enough: a page that functions but looks like browser defaults is not done.

Build from what the project already has. If it has design tokens, a theme, a
Tailwind config or a shared component package, use and extend those rather than
inventing parallel styles or one-off values. Where a shared component falls
short of the rules below, fix it in the shared component rather than patching
around it in one page. Apply this bar to the UI the task touches; do not restyle
screens the task does not touch.

1. Typography. Use a deliberate type scale with visible hierarchy: page title,
   section heading, body and caption differ clearly in size and weight. Key
   values (prices, totals, counts) stand out. Do not leave everything in the
   default system font at one size.
2. Colour. Neutrals carry the page: backgrounds, text, borders. Brand colours
   are accents for primary actions, active states and highlights, not every
   link, badge and underline at full saturation. Text meets WCAG AA contrast.
3. Spacing and depth. Use one consistent spacing scale and give sections room
   to breathe. Separate surfaces with subtle elevation, not only a 1px grey
   border. Every interactive element has hover, focus-visible, active and
   disabled states.
4. No empty boxes. Every image slot has a designed fallback (placeholder,
   initials, an illustration or a colour block), never a blank space. Every
   list and table has an empty state saying what is going on and what to do
   next. Every view that loads data has a loading state shaped like the content
   and an error state a user can act on.
5. Content, not data. Never show raw enum values, database keys, snake_case,
   ISO timestamps or unformatted numbers. Map them to human labels ("Service",
   not "service"), show dates in a readable form, and format money with its
   symbol and separators.
6. Layout. Works at phone width with no horizontal scroll, and at desktop width
   with a sensible maximum content width. Each page leads with its main purpose
   or action.
7. Accessibility. Use semantic elements, label every form field, give images
   alt text, and keep everything reachable by keyboard.

Before you finish, look at each screen you touched as a user would and check it
against these rules. Fix what falls short. Name in your summary any rule you
could not meet, and why.
