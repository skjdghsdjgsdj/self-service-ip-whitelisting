# Self-service IP whitelisting for Caddy

## What is this?

Say you have something in [Caddy](https://caddyserver.com/) that you only want specific IPs to access, but those IPs can change from time to time. It would be tedious to manage the list of trusted IPs yourself, so this script lets your users self-service that management. If a user changes their location and gets a new IP, then they can hit an endpoint to become trusted again and no longer trust the old location.

## What does it do?

When Caddy receives a request, it uses [`forward_auth`](https://caddyserver.com/docs/caddyfile/directives/forward_auth) to forward the request onto a Python microservice first. If the microservice says it's trusted, then the request continues normally. If not, the request gets blocked by Caddy. All this is transparent to whatever you're actually hosting on Caddy.

Separately, users can log in via a system you manage to have their current IP trusted.

## How does it work?

User IPs get persisted into Redis, along with metadata defining who owns them.

It exposes several endpoints:

* `/check` is used by `forward_auth` to check the current request against whitelisted IPs. It responds with `204` if the request is accepted (a key exists in Redis) or a `4xx` code if denied or on errors.
* `/trust_me` is what a user invokes to become trusted. This endpoint must be protected by Authentik or another authorization provider. Hitting this endpoint stores the user's IP in Redis and deletes the old one, if any. It responds with `201` on success, even if the IP is already trusted, or `4xx`/`500` on errors depending on what went wrong.
* `/health` is a health check endpoint: `200` on healthy and a non-`2xx` code on unhealthy.

To somewhat visualize it, here's how it might work in your environment when someone makes a request to a resource you've protected:

1. Request hits Cloudflare.
2. Cloudflare adds its `X-Forwarded-For` header pointing to the user's actual IP, then sends it onto your Caddy origin.
3. Caddy routes the request to the correct host, which has a `forward_auth` block for `localhost:5554/check`
4. The Python microservice running there reads the `X-Forwarded-For` header and sees if a matching key in Redis exists:
   1. If it does, the microservice responds with 2xx, at which point Caddy continues processing the request normally, however you defined that.
   2. If it doesn't, the microservice responds with 4xx. Caddy stops processing the request and responds to the user with that status code.

Here's how the flow works when a user wants to have their IP trusted:

1. Request hits Cloudflare.
2. Cloudflare adds its `X-Forwarded-For` header pointing to the user's actual IP, then sends it onto your Caddy origin.
3. Caddy sends the request to Authentik's proxy provider (something you need to set up yourself).
4. Authentik sends the request to the `localhost:5554/trust_me` endpoint, and in the process, injects its `X-authentik-username` header for the currently logged in user. If the user isn't logged in, they get redirected to the login.
5. The microservice looks up the user's old trusted IP if it exists and deletes the Redis key, then inserts a new one for the new IP in `X-Forwarded-For` and stores metadata saying "this IP is for the username defined in `X-authentik-username`".
6. The microservice responds with 2xx, and now the user can hit endpoints locked down with `forward_auth` normally.

Think of this as a tool in your toolchain for authentication and authorization; it just handles the logistics of managing and checking IP addresses. Managing the trust access itself is up to you via Authenik or whatever else you're using, and in that system is where you'll manage the actual authorization of "is this user allowed to log in and are they allowed to manage their trusted IPs."

You can do fancy automation if you want to extend the system, like connecting to an externally-managed Redis instance and listening for key changes, then notifying someone when trust relationships change. All that is pretty easy to do via [n8n](https://n8n.io/), for example. The microservice is meant to be barebones and for you to integrate, not to be a power system with its own UX.

## What do I need?

To run this, you need:

* Docker Compose (technically optional, but easiest)
* Caddy, or another web server capable of forward authorization
* [Authentik](https://goauthentik.io/) or something else that provides authentication and authorization, and can function as a reverse proxy once a user is authenticated

If you're so inclined, you can also just run the Python app directly. It is a normal Flask microservice and you can install dependencies via `pip install -r requirements.txt`.

You can also connect to a separately managed Redis instance by changing `docker-compose.yml` or the values in `.env`. If you do that, the ACL for the user looks like:

```
user foo on >bar +@read +@write +@transaction +ping ~ip-whitelist:*
```

Change `foo` to the username, `bar` to the password, and if you changed `REDIS_PREFIX`, change `ip-whitelist` to that. There's no need to use an external Redis instance unless you want to do some kind of integration beyond what the microservice can do.

## How do I set it up?

First, launch the microservice and make sure it's working.

1. Clone this repository somewhere.
2. Copy `.env.example` to `.env` and edit it per its comments.
3. Launch the service with `docker compose up -d`. This builds the microservice and deploys it to listen on `0.0.0.0:5554`.
4. Test that the service is working properly: run `curl http://localhost:5554/health` and make sure it responds with "OK" in a JSON object.

Then you need to protect the endpoints. There are many ways of doing this, so it's up to you to choose what works best with your environment. But however you do it:

* `/check` should only be accessible to Caddy itself.
* `/trust_me` must be protected by Authentik or another reverse proxy capable authorization solution. Such a solution must be able, on an authenticated request, send an HTTP header along with the username/email/etc. of the currently logged in user. In Authentik, you would do this with a reverse proxy provider pointing to the microservice and use the `X-authentik-username` header. **This is the only endpoint that should be internet-accessible.** Be sure this endpoint itself doesn't get dependent on itself (i.e., don't use it where hitting `/trust_me` could actually hit the endpoint recursively).
* `/health` should only be accessible to a health checker like [Uptime Kuma](https://uptime.kuma.pet/) if you want to monitor it.

And then the important part: integrate it into Caddy. A basic configuration would look like this.

```
forward_auth {
    uri localhost:5554/check # change if running on a separate host than Caddy
    # If the header containing the actual IP isn't X-Forwarded-For or another header that
    # Caddy automatically sends to the forward_auth endpoint, you'll need to add it, like:
    #copy_headers X-Foo
}

# stuff here only gets invoked if the forward_auth passes
# reverse_proxy ...
# file_server ...
# etc.
```

If you have static IPs or subnets you always want to allow, add them as subnets to `TRUSTED_SUBNETS` in `.env` and restart the service. For example, you might always want to implicitly trust LAN traffic, but require explicit trust of internet traffic. You should do this for all static IPs/subnets instead of explicitly trusting them because it skips checking in Redis. If you want to trust a specific IP, add it as a `/32` (IPv4).

## What's left?

It's a very simplistic solution by design, so it doesn't make for a great UX out of the box. But, it's easy to wrap it in a nice UI if you want: a button that just hits `/trust_me` is all you really need.

I have no idea how well this scales, but probably decently well with some changes. I wouldn't call it "enterprise-ready." I have no idea how well it would do against a DDoS or something. But, if you're using this in an enterprise context, you're doing something very wrong and should build a more mature system.

Remember this blocking happens at the application layer, not network layer, so IPs that aren't on the whitelist are still able to make requests; they just get blocked by Caddy. That increases your risk profile over network blocking, so network blocking is preferable if your use case supports it.

Submit issues and PRs if you have suggestions on improving this.