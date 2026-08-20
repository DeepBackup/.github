import unittest
from unittest.mock import patch, MagicMock
import requests

import mirror_deepbackup


class TestMirrorDeepbackup(unittest.TestCase):

    @patch("mirror_deepbackup.time.sleep")
    @patch("mirror_deepbackup.requests.request")
    def test_make_request_retry_on_502(self, mock_request, mock_sleep):
        resp_502 = MagicMock()
        resp_502.status_code = 502

        resp_200 = MagicMock()
        resp_200.status_code = 200

        mock_request.side_effect = [resp_502, resp_200]

        res = mirror_deepbackup.make_request("POST", "https://api.github.com/graphql", retries=3, backoff_factor=0.1)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once_with(0.1)

    @patch("mirror_deepbackup.make_request")
    def test_get_starlist_repositories_success(self, mock_make_request):
        # Mock initial list query response
        lists_response = MagicMock()
        lists_response.raise_for_status.return_value = None
        lists_response.json.return_value = {
            "data": {
                "viewer": {
                    "lists": {
                        "nodes": [
                            {"id": "list_123", "name": "DB"}
                        ]
                    }
                }
            }
        }

        # Mock items pagination response
        items_response = MagicMock()
        items_response.raise_for_status.return_value = None
        items_response.json.return_value = {
            "data": {
                "node": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "owner": {"login": "octocat"},
                                "name": "Hello-World",
                                "url": "https://github.com/octocat/Hello-World",
                                "isPrivate": False
                            }
                        ]
                    }
                }
            }
        }

        mock_make_request.side_effect = [lists_response, items_response]

        repos = mirror_deepbackup.get_starlist_repositories("test-token", "DB")
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["owner"], "octocat")
        self.assertEqual(repos[0]["name"], "Hello-World")

    @patch("mirror_deepbackup.make_request")
    def test_get_starlist_repositories_not_found(self, mock_make_request):
        lists_response = MagicMock()
        lists_response.raise_for_status.return_value = None
        lists_response.json.return_value = {
            "data": {
                "viewer": {
                    "lists": {
                        "nodes": [
                            {"id": "list_123", "name": "OtherList"}
                        ]
                    }
                }
            }
        }
        mock_make_request.return_value = lists_response

        with self.assertRaises(mirror_deepbackup.GraphQLError) as ctx:
            mirror_deepbackup.get_starlist_repositories("test-token", "DB")
        self.assertIn("Star List 'DB' not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
