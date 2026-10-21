
I want to create a multiplayer version of dots and boxes running on my tuffy badge.

PowerSync will be driving the multiplayer components. I've gotten PowerSync to run on my tuffy badge (that I got at supabase select). You can see the reference code in ../badgegotchi

How the dots and boxes should work is:

There will be two main components: a web dashboard for creating game rooms and facilitate badge pairing, and then the badgeware badge app

 web dashboard 
this is a web app anyone can use and access using supabase anonymous auth. no email to sign in. React app hosted on Vercel (already linked) and backed by Supabase (also already linked with CLI) with powersync for realtime. 

there are three main use cases:
1) watch an existing room (read only view mode - streaming)
2) create a new room 
3) join a room


when you create a new room, it should create a room player and show a pairing code for you to enter on your badge, and when someone else selects that room to join, it should show their unique pairing code. we make the codes easy to type in using ABC buttons, and it doesn't have to be hack proof just like 8 bits of entropy is fine.

users can only create one room until the room is played at which point they can create another.

so the powersync sync streams can be set up to so that a user sees their "active" room that they have joined, and similarly sync stream parameters can be injected from the client to watch any existing room. 

for first time users, we can show them installation instructions to load the game onto their badge.

badge app
the badge app runs using the powersync native SDK, and sqlite using sqlite-littlefs-vfs , a project I created. the app is sideloaded when the badge is placed in USB drive mode. you can view a working reference project in ../badgegotchi

there is also an already working version of the badge app in ../powerbadge , you can use some of that reference code for this project and update it to work with powersync

also everything should be linked: powersync, vercel and supabase
