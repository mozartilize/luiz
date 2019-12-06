package main

import (
	"fmt"
	log "github.com/sirupsen/logrus"
	"io/ioutil"
	"net/http"
	"net/url"
	"os"

	"github.com/joho/godotenv"

	"github.com/gorilla/sessions"
	json "github.com/json-iterator/go"
	"github.com/labstack/echo-contrib/session"
	"github.com/labstack/echo/v4"
	uuid "github.com/satori/go.uuid"

	sq "github.com/Masterminds/squirrel"
	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"
)

var db *sqlx.DB

func getDB() *sqlx.DB {
	var err error
	db, err = sqlx.Open("sqlite3", "./test.db")
	if err != nil {
		fmt.Println("Cant connect database")
	}
	return db
}

func getPostgreDB() (*sqlx.DB, error) {
	var err error
	if db == nil {
		db, err = sqlx.Open("postgres", os.Getenv("DATABASE_URL"))
		if err != nil {
			panic(err)
		}
		db.SetMaxOpenConns(15)
		db.SetConnMaxLifetime(10 * 60 * 1e9)
	}
	return db, nil
}

func main() {
	if err := godotenv.Load(); err != nil {
		log.Warn("Error loading .env file: " + err.Error())
	}
	e := echo.New()
	cookieStore := sessions.NewCookieStore([]byte("secret"))
	cookieStore.Options = &sessions.Options{
		Path:     "/",
		MaxAge:   10 * 60, // 10mins because the code from response lasts 10mins
		HttpOnly: true,
	}
	e.Use(session.Middleware(cookieStore))

	e.GET("/", func(c echo.Context) error {
		return c.String(http.StatusOK, "Hello, World!")
	})
	e.GET("/slack/login", login)
	e.GET("/slack/auth", auth)
	e.Logger.Fatal(e.Start(":1323"))
}

func login(c echo.Context) error {
	baseURL, _ := url.Parse("https://slack.com/oauth/authorize")
	params := url.Values{}
	params.Set("client_id", os.Getenv("SLACK_CLIENT_ID"))
	params.Set("scope", "bot admin links:read links:write chat:write:user chat:write:bot files:read")
	var scheme string
	if c.Request().TLS != nil || c.Request().Header.Get("X-Forwarded-Proto") == "https" {
		scheme = "https"
	} else {
		scheme = "http"
	}
	params.Set("redirect_uri", scheme+"://"+c.Request().Host+"/slack/auth")
	params.Set("state", uuid.NewV4().String())
	baseURL.RawQuery = params.Encode()
	sess, _ := session.Get("session", c)

	sess.Values["slack:state"] = params.Get("state")
	log.Info(params.Get("state"))
	sess.Values["slack:redirect_uri"] = params.Get("redirect_uri")
	sess.Save(c.Request(), c.Response())
	c.Response().Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")
	return c.Redirect(http.StatusMovedPermanently, baseURL.String())
}

func auth(c echo.Context) error {
	c.Response().Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")

	authError := c.QueryParam("error")

	if authError != "" {
		return c.String(http.StatusUnauthorized, "Unauthorized")
	}

	state := c.QueryParam("state")
	sess, _ := session.Get("session", c)
	sessionState, _ := sess.Values["slack:state"]
	if sessionState == nil {
		sessionState = ""
	}
	if state != sessionState {
		log.Error(state + " " + sessionState.(string))
		return c.String(http.StatusConflict, "State does not match")
	}

	redirectURI, _ := sess.Values["slack:redirect_uri"]
	if redirectURI == nil {
		redirectURI = ""
	}

	clientID := os.Getenv("SLACK_CLIENT_ID")
	clientSecret := os.Getenv("SLACK_CLIENT_SECRET")
	code := c.QueryParam("code")
	if code == "" {
		return c.String(http.StatusUnauthorized, "Unauthorized")
	}

	if resp, err := http.PostForm("https://slack.com/api/oauth.access", url.Values{
		"client_id":     []string{clientID},
		"client_secret": []string{clientSecret},
		"code":          []string{code},
		"redirect_uri":  []string{redirectURI.(string)},
	}); err == nil {
		if body, err := ioutil.ReadAll(resp.Body); err == nil {
			var data map[string]interface{}
			json.Unmarshal(body, &data)
			if data["ok"] == true {
				fmt.Println(data["access_token"])
				fmt.Println(data["team_id"])
				db, _ = getPostgreDB()
				sql := sq.Insert("tokens").
					Columns("team_id", "access_token").
					Values(data["team_id"], data["access_token"])
				if _, err := sql.RunWith(db).PlaceholderFormat(sq.Dollar).Exec(); err != nil {
					log.Info(sql.ToSql())
					log.Error(err)
					return c.String(http.StatusInternalServerError, err.Error())
				}
			} else {
				return c.String(http.StatusUnauthorized, "Grant access fail: "+data["error"].(string))
			}
		} else {
			return c.String(http.StatusUnauthorized, "Grant access fail: "+err.Error())
		}
	} else {
		fmt.Println(err)
		return c.String(http.StatusUnauthorized, "Unauthorized")
	}
	return c.String(http.StatusOK, "OK!")
}
