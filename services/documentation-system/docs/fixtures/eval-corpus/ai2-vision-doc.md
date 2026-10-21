# AI² tournament vision document

Status legend: **Finalized** — sections 1 through 4. **Ongoing** — sections 5 through 7.

## 1. Introduction

AI² is a machine learning tournament run by UTMIST. Teams of students compete to design
the strongest reinforcement learning agent inside an in-house platform fighting game.
The tournament runs as an elimination bracket, with consecutive rounds of agents facing
off one against one until a single winner remains.

The event runs eight days, from October 25 to November 2, with in-person days at each
end and drop-in work and networking sessions through the week between them. The opening
day carries networking and workshops, including an introduction to reinforcement
learning. Virtual support runs for remote participants throughout, and tournament coding
is asynchronous until the submission deadline of November 1 at 23:59. The final day is a
double-elimination bracket of the top agents, followed by panels, speakers and awards.

The tournament aims to build engagement with machine learning across every level of
experience, in a competitive but educational setting that asks participants to ship
something working to a real standard.

## 2. Mission

To provide a beginner-friendly tournament where students build a community and extend
their skills in AI and ML. Participants get workshop instruction in the fundamentals, a
rich environment to test against, and a cohort of peers to collaborate and compete with.
A secondary aim is to show what paths exist for AI work in industry, the games industry
included.

## 3. Goals and objectives

- **Provide a reinforcement learning foundation.** Give attendees working knowledge of
  RL frameworks and an unusual, concrete way to start.
- **A simple but rich challenge.** An environment a beginner can solve and an
  experienced participant can exploit.
- **Community engagement.** Put students and mentors of differing skill in the same
  room.
- **Introduce UTMIST.** Establish the values and the ways to get involved, implicitly
  and explicitly.

## 4. Target audience

Three demographics:

- **Beginners** — new to RL and AI, at the start of it.
- **Enthusiasts** — some experience through coursework, internships or research, and
  looking to go deeper.
- **Emerging experts** — experienced, weighing next steps in academia or industry.

## 5. Workshops

### 5.1 Focus

Workshops sit mostly on the kickoff day and cover topics in reinforcement learning. They
are beginner-oriented and meant as a starting point for novice participants. They relate
to the RL workshop series run through the first semester, but in condensed form and
aimed directly at the tournament task.

### 5.2 Workshop list

**Workshop 1 — Introduction to the RL paradigm.** A general introduction to
reinforcement learning for participants new to the field.

**Workshop 2 — Codebase walkthrough and work session.** Feedback from last year was that
participants struggled to understand the codebase. This session walks through training
an agent end to end: defining reward functions, using the provided baselines, defining a
custom network where wanted, and running training.

**Workshop 3 — Guest speaker.** Speaker to be confirmed.

**Workshop 4 — Research.** Format to be confirmed.

## 6. Agenda and planning

### 6.1 Kickoff day

Date: October 25. Location: ES1050, pending confirmation. Full-day kickoff with sponsor
booths and industry workshops.

| Time | Event | Room requirements |
| --- | --- | --- |
| 09:30-10:00 | Registration | |
| 10:00-10:30 | Opening ceremony | |
| 10:30-11:00 | Networking and team-building, in person and on Discord | |
| 11:00-12:00 | Workshop 1: intro to the RL paradigm | outlets |
| 12:00-12:30 | Codebase walkthrough | outlets |
| 12:30-13:30 | Lunch | |
| 13:30-14:30 | Workshop 2 | |
| 14:45-15:45 | Workshop 3 | |
| 15:45-16:30 | Work session, mentoring and networking | |
| 16:30-17:00 | Closing ceremony | |

### 6.2 Mentoring sessions

Optional, hybrid, running between the two in-person days.

| Date | Format |
| --- | --- |
| October 26 | Online |
| October 27 | Online |
| October 28 | In person |
| October 29 | Online |
| October 30 | Online |
| November 1 | In person |

### 6.3 Finals day

Date: November 2. Location to be confirmed. Finals run with an esports format, with
merch handout and extended networking.

| Time | Event | Notes |
| --- | --- | --- |
| 09:30-10:00 | Registration | |
| 10:00-10:30 | Opening ceremony | |
| 10:30-11:00 | Round 1, six matches | |
| 11:00-12:00 | Games industry panel | local panellists |
| 12:00-13:00 | Lunch | |
| 13:15-13:45 | Round 2, five matches | lobby |
| 14:00-15:00 | Games industry speaker | |
| 15:00-15:15 | Round 3, two matches | sponsor shoutouts, alumni |
| 15:15-16:00 | Networking | |
| 16:00-17:00 | Finals, awards, keynote, closing | |

Open items for the finals: networking booths for annual sponsors, the main finals
speaker, the panel lineup, prizes, and whether the tournament is livestreamed.

### 6.4 Compute

Last year's allocation was 4 GB of RAM and CPU only, which proved too slow. The revised
plan for this year:

- A step cap of 2,700 per match. At roughly 50 ms per step across two agents that is
  about 4.5 minutes of wall clock per game.
- Roughly twelve games an hour at five minutes a game, assuming one GPU per game.
- An upper bound of 100,000 parameters per submitted agent.
- No training during the tournament itself; submissions are evaluated as given.
- Two GPUs provisioned.

Memory is covered by the parameter cap rather than a separate limit, and CPU load stays
light since the work is mostly moving a model onto the GPU.

### 6.5 Todo

Speaker list, split by industry and academia, with contacts and priority. Confirmation
of the finals venue. Final prize structure.

## 7. Roles and responsibilities

| Name | Position | Responsibility |
| --- | --- | --- |
| Devon Clarke, Mei Lin Ortiz | Co-president | Vision and coordination, support |
| Samir Haddad, Noor Abbasi | VP Academics | Event oversight, logistics, marketing, finance, workshop supervision, sponsorship outreach |
| Tomas Brandt, Iris Nakamura | VP Infrastructure | Infrastructure leads, host and submission platform, website |
| To be assigned | Infrastructure directors | Website, tournament display, submission handling |
| Rosa Delgado | Academics developer lead | Environment and game development |
| To be assigned | Academics developers | Environment and game development |
| Hana Vuong | Academics director | Workshops |

### 7.1 Department responsibilities

**Co-presidents.** Own the vision for the event, oversee planning end to end, and
coordinate across departments and external partners.

**Logistics.** Venue booking, floor plans, equipment, catering, event registration, and
on-the-day execution. Owns the logistics budget.

**Marketing.** Promotion strategy and execution, and the event's brand guideline.

**External relations.** Finalizes the sponsorship package, runs outreach and cold
contacts, and confirms sponsorship agreements and deliverables.

**Internal affairs.** Financial planning and budgeting, grant applications, and the
sponsorship tier costs that decide what the event can fund.

**Chief of staff.** Communication and membership coordination, including the role
general membership plays in the event.

**Infrastructure.** The dedicated event page, and registration setup with logistics.

**Technical writing.** Workshop planning and materials, and hosting the workshops.
Materials should target beginners.

**Engineering.** Booth coordination, guidelines and posters.
