# agent-grid

The goal of this project is to create a system that orchestrates a series of coding
agents to autonomously execute work as specified in GitHub issues.  The key design
principles of this system are:

- **Optimistic execution:** coding agents proactively and without human intervention
  _attempt_ work
- **Issue tracker as centralized state and coordination:** GitHub is used to track the 
  state of all execution (using GitHub's comments and description functionality) and to
  coordinate work - execution graphs are modeled using GitHub's issue relationship and
  parent / subissue concepts.  Agent and human coordinate using GitHub issue/PR
  assignment.
- **Agents are general purpose:** no distinction is drawn between orchestration, planning and
  execution.
  - When agents pick up an issue, it decides using it's own prompting whether
    the task is too large to execute in one shot.  If so, then it creates subissues
    to model the work breakdown.
  - Orchestration is done by periodically having an agent "resume" a parent issue.  It
    examines the children issues and the agents' progress against those to decide what
    if anything else needs to be done. 
- **Agent execution is one-shot and short-lived:** Agent execution is designed so that
  it can complete its task without further intervention and in a short amount of time.
  Long-range goal completion is accomplished by: a/ breaking work down into smaller tasks
  that can be one shot (agents can decide if the next best action for a given task is
  simply create subtasks) b/ periodically "resuming" the parent task to check on the
  overall status.  In this sense it's a little like a large "polling" loop and therefore
  is stateless.

## Architecture

There are the following components of the system:

- `execution-grid`: responsible for actually launching the agents to run.  Eventually
  will be hosted on cloud infrastructure in MicroVMs, but for now, should simply
  launch subprocesses locally.
  - Status events (including final state) should be published to a SQS-compatible event
    bus
- `coordinator`: the central service that is responsible for when and how to start
  executions on the grid.  Naively this should simply map work (in the form of GitHub
  issues) and launch agent executions with the appropriate prompt.
    - For now, this should also host the backend logic for any analytics or UI for a
      user to inspect the system.  But this may grow large enough over time to be
      separated into its own module   
    - The coordinator should also handle a "nudge" operation. For instance, when one
      agent completes a task, clearly the _next_ peer subissue should be started. 
      Rather than this being centrally coordinated, each agent should be able to "nudge"
      the system to prioritize an agent execution against that next subissue.  It's a
      "nudge" in the sense that it's not a guarantee (the coordinator should manage
      budgets and safety concerns to avoid runaway jobs). 
    - The coordinator must also decide when and how to run the management agents – that
      check in on long-running (parent) issues and make sure sub tasks are going well.
      A simple polling-style timeout could be acceptable in V1.
 - `issue-tracker`: GitHub will likely not be the only target one day. Encapsulating the
   issue tracker behind its own module seems wise.  This should also handle eventing
   from GitHub.
     - Not every GitHub issue should necessarily start an agent.  In this sense, these
       incoming events should be considered "nudges" as well (as per above).

## Technical Notes

- The backend should be implemented using Python 3.12
  - If an HTTP server is needed, please use FastAPI
- "Public" API of modules should be prefixed with `public_` in the filename